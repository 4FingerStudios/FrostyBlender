using System;
using System.IO;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FrostyBlender.Services;

namespace FrostyBlender.ViewModels;

public partial class MainViewModel
{
    private AppRelease? _latestAddonRelease;
    private string _runningAddonVersion = "";
    private string? _addonRestartVersion;
    [ObservableProperty] private string _addonUpdateStatus = "Check for the latest Blender add-on in GitHub Releases.";
    [ObservableProperty] private bool _addonUpdateAvailable;
    [ObservableProperty] private bool _addonUpdateBusy;

    private void RefreshAddonConnection(JsonObject? ping)
    {
        var running = ping?["addon_version"]?.GetValue<string>() ?? _link.Info?.AddonVersion ?? "";
        var pending = ping == null ? _addonRestartVersion : ping["restart_required"]?.GetValue<string>();
        if (running == _runningAddonVersion && pending == _addonRestartVersion) return;
        _runningAddonVersion = running;
        _addonRestartVersion = pending;
        if (!AddonUpdateBusy) RefreshAddonUpdateStatus();
    }

    private void RefreshAddonUpdateStatus()
    {
        AddonUpdateAvailable = false;
        if (_addonRestartVersion != null)
        {
            AddonUpdateStatus = $"Add-on {_addonRestartVersion} installed. Save your work and restart Blender to activate it.";
            return;
        }
        if (_latestAddonRelease == null) return;
        if (!Version.TryParse(_runningAddonVersion, out var installed))
        {
            AddonUpdateStatus = $"Latest add-on: {_latestAddonRelease.Version}. Install the add-on from Releases and open Blender first.";
            return;
        }
        AddonUpdateAvailable = _latestAddonRelease.Version > installed;
        AddonUpdateStatus = AddonUpdateAvailable
            ? $"Add-on {_latestAddonRelease.Version} is available (installed: {installed})."
            : $"Add-on {installed} is up to date.";
    }

    [RelayCommand]
    private async Task CheckForAddonUpdates()
    {
        if (AddonUpdateBusy) return;
        AddonUpdateBusy = true;
        AddonUpdateStatus = "Checking GitHub for add-on updates...";
        try
        {
            _latestAddonRelease = await _updater.CheckAddonAsync();
            _runningAddonVersion = _link.Info?.AddonVersion ?? _runningAddonVersion;
            RefreshAddonConnection(await _link.Get("/ping"));
            RefreshAddonUpdateStatus();
        }
        catch (Exception error) { AddonUpdateStatus = "Could not check add-on updates: " + error.Message; }
        finally { AddonUpdateBusy = false; }
    }

    [RelayCommand]
    private async Task InstallAddonUpdate()
    {
        if (AddonUpdateBusy || UpdateBusy || _latestAddonRelease == null || !AddonUpdateAvailable) return;
        AddonUpdateBusy = true;
        string? archive = null;
        var submitted = false;
        var terminal = false;
        try
        {
            if (Sending || _job != null) throw new InvalidOperationException("Wait for the current import to finish.");
            var ping = await _link.Get("/ping") ?? throw new InvalidOperationException("Open Blender with the add-on enabled first.");
            if (ping["addon_update"]?.GetValue<bool>() != true)
                throw new InvalidOperationException("Install add-on 1.10.2 or newer manually once and restart Blender to enable automatic add-on updates.");
            RefreshAddonConnection(ping);
            if (_addonRestartVersion != null) throw new InvalidOperationException("Save your work and restart Blender to finish the previous update.");
            var folder = Path.Combine(BlenderLink.Folder, "updates");
            Directory.CreateDirectory(folder);
            archive = Path.Combine(folder, $"frostbite_importer-{_latestAddonRelease.Version}-{Guid.NewGuid():N}.zip");
            await _updater.DownloadAsync(_latestAddonRelease, archive,
                new Progress<int>(percent => AddonUpdateStatus = $"Downloading add-on... {percent}%"));
            string digest;
            await using (var file = File.OpenRead(archive))
                digest = "sha256:" + Convert.ToHexString(await SHA256.HashDataAsync(file));
            AddonUpdateStatus = "Waiting for Blender to install the add-on...";
            // Keep the archive if the bridge times out: Blender might already have accepted the request.
            submitted = true;
            var job = await _link.UpdateAddon(new JsonObject
            {
                ["archive"] = archive, ["digest"] = digest, ["version"] = _latestAddonRelease.Version.ToString(3)
            });
            var deadline = DateTime.UtcNow.AddMinutes(2);
            while (DateTime.UtcNow < deadline)
            {
                await Task.Delay(500);
                var status = await _link.Get("/status");
                var result = status?["jobs"]?[job];
                var state = result?["state"]?.GetValue<string>();
                if (state is not ("done" or "failed")) continue;
                terminal = true;
                if (state == "failed") throw new InvalidOperationException(result?["message"]?.GetValue<string>() ?? "Blender rejected the update.");
                _addonRestartVersion = _latestAddonRelease.Version.ToString(3);
                RefreshAddonUpdateStatus();
                return;
            }
            throw new TimeoutException("Blender has not confirmed installation yet. Keep Blender open and check its connection before retrying.");
        }
        catch (Exception error) { AddonUpdateStatus = "Could not update the add-on: " + error.Message; }
        finally
        {
            AddonUpdateBusy = false;
            if (archive != null && (!submitted || terminal))
            {
                try { File.Delete(archive); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }
}
