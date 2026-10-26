using System;
using System.IO;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FrostyBlender.Services;

namespace FrostyBlender.ViewModels;

public partial class MainViewModel
{
    private readonly AppUpdater _updater = new();
    private AppRelease? _availableRelease;
    [ObservableProperty] private string _updateStatus = "Updates are checked on startup.";
    [ObservableProperty] private bool _updateBusy;
    [ObservableProperty] private bool _updateAvailable;
    [ObservableProperty] private bool _checkUpdatesOnStartup = true;

    partial void OnCheckUpdatesOnStartupChanged(bool value)
    {
        _settings.CheckUpdatesOnStartup = value;
        _settings.Save();
    }

    private void InitializeUpdates()
    {
        CheckUpdatesOnStartup = _settings.CheckUpdatesOnStartup;
        // A dev build would be replaced by the release app and add-on, which leave Unreal out: no startup checks.
        if (UnrealEnabled) return;
        if (CheckUpdatesOnStartup) _ = CheckForAddonUpdates();
        var log = Path.Combine(BlenderLink.Folder, "update.log");
        if (File.Exists(log))
            UpdateStatus = "The previous update failed. See " + log + ". You can retry or download from Releases.";
        else if (CheckUpdatesOnStartup)
            _ = CheckForUpdates();
    }

    [RelayCommand]
    private async Task CheckForUpdates()
    {
        if (UpdateBusy) return;
        UpdateBusy = true;
        UpdateStatus = "Checking GitHub for updates...";
        try
        {
            var release = await _updater.CheckAsync();
            _availableRelease = release.Version > Version.Parse(AppVersion) ? release : null;
            UpdateAvailable = _availableRelease != null;
            UpdateStatus = UpdateAvailable ? $"FrostyBlender {release.Version} is available (installed: {AppVersion})."
                : $"FrostyBlender {AppVersion} is up to date.";
        }
        catch (Exception error) { UpdateStatus = "Could not check for updates: " + error.Message; }
        finally { UpdateBusy = false; }
    }

    [RelayCommand]
    private async Task InstallUpdate()
    {
        if (UpdateBusy || AddonUpdateBusy || _availableRelease == null) return;
        if (Sending)
        {
            UpdateStatus = "Wait for the current import to finish before updating.";
            return;
        }
        UpdateBusy = true;
        string? staged = null;
        var handedOff = false;
        try
        {
            var desktop = Application.Current?.ApplicationLifetime as IClassicDesktopStyleApplicationLifetime
                ?? throw new InvalidOperationException("The desktop app is not running.");
            var target = AppUpdater.InstalledExecutable();
            // Stage beside the executable, checking write permissions before the running app exits.
            staged = target + ".update-" + Guid.NewGuid().ToString("N");
            UpdateStatus = "Downloading update...";
            await _updater.DownloadAsync(_availableRelease, staged,
                new Progress<int>(percent => UpdateStatus = $"Downloading update... {percent}%"));
            if (Sending) throw new InvalidOperationException("An import started during the download. Finish it and try again.");
            UpdateStatus = "Restarting to install the update...";
            AppUpdater.StartInstaller(target, staged);
            handedOff = true;
            desktop.Shutdown();
        }
        catch (Exception error)
        {
            UpdateStatus = "Could not install the update: " + error.Message + " You can also download it from Releases.";
        }
        finally
        {
            if (!handedOff && staged != null)
            {
                try { File.Delete(staged); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            UpdateBusy = false;
        }
    }
}
