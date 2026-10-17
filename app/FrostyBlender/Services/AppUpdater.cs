using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace FrostyBlender.Services;

public sealed record AppRelease(Version Version, Uri DownloadUrl, long Size, string? Digest, bool IsAddon = false);

/// <summary>Updates the portable Windows app from the latest stable GitHub release.</summary>
public sealed class AppUpdater
{
    public const string ReleasesUrl = "https://github.com/4FingerStudios/FrostyBlender/releases/latest";
    private readonly HttpClient _http;
    public AppUpdater(HttpClient? http = null) => _http = http ?? new HttpClient();

    public Task<AppRelease> CheckAddonAsync(CancellationToken token = default) => CheckAsync(token, true);

    public async Task<AppRelease> CheckAsync(CancellationToken token = default, bool addon = false)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(20));
        using var request = new HttpRequestMessage(HttpMethod.Get,
            "https://api.github.com/repos/4FingerStudios/FrostyBlender/releases/latest");
        request.Headers.UserAgent.ParseAdd("FrostyBlender-Updater/1.0");
        request.Headers.Accept.ParseAdd("application/vnd.github+json");
        using var response = await _http.SendAsync(request, timeout.Token);
        response.EnsureSuccessStatusCode();
        return ParseRelease(await response.Content.ReadAsStringAsync(timeout.Token), addon);
    }

    public static AppRelease ParseRelease(string json, bool addon = false)
    {
        var release = JsonNode.Parse(json) ?? throw new InvalidDataException("Empty release response.");
        if (release["draft"]?.GetValue<bool>() == true || release["prerelease"]?.GetValue<bool>() == true)
            throw new InvalidDataException("The latest release is not a stable release.");
        // Release tags follow the Blender add-on, which has an independent version.
        var assets = release["assets"]?.AsArray() ?? throw new InvalidDataException("Release has no assets.");
        var candidates = assets.Select(asset =>
        {
            var match = Regex.Match(asset?["name"]?.GetValue<string>() ?? "",
                addon ? @"^frostbite_importer-(\d+\.\d+\.\d+)-windows-x64\.zip$"
                    : @"^FrostyBlender-(\d+\.\d+\.\d+)-windows-x64\.exe$", RegexOptions.CultureInvariant);
            return (Asset: asset, Match: match);
        }).Where(x => x.Match.Success).OrderByDescending(x => Version.Parse(x.Match.Groups[1].Value));
        var candidate = candidates.FirstOrDefault();
        if (candidate.Asset == null) throw new InvalidDataException($"The latest release has no Windows x64 {(addon ? "add-on" : "app")}. Try again after its upload finishes.");
        var url = new Uri(candidate.Asset["browser_download_url"]!.GetValue<string>());
        if (url.Scheme != "https" || url.Host != "github.com" ||
            !url.AbsolutePath.StartsWith("/4FingerStudios/FrostyBlender/releases/download/", StringComparison.Ordinal))
            throw new InvalidDataException("Unexpected release download URL.");
        var size = candidate.Asset["size"]?.GetValue<long>() ?? 0;
        if (size <= 0) throw new InvalidDataException("The release download is empty.");
        return new AppRelease(Version.Parse(candidate.Match.Groups[1].Value), url, size,
            candidate.Asset["digest"]?.GetValue<string>(), addon);
    }

    public async Task DownloadAsync(AppRelease release, string destination, IProgress<int>? progress = null,
        CancellationToken token = default)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromMinutes(10));
        var created = false;
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, release.DownloadUrl);
            request.Headers.UserAgent.ParseAdd("FrostyBlender-Updater/1.0");
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
            response.EnsureSuccessStatusCode();
            await using (var input = await response.Content.ReadAsStreamAsync(timeout.Token))
            await using (var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                created = true;
                var buffer = new byte[81920];
                long total = 0;
                int count;
                while ((count = await input.ReadAsync(buffer, timeout.Token)) > 0)
                {
                    total += count;
                    if (total > release.Size) throw new InvalidDataException("Download exceeds the release's size.");
                    await output.WriteAsync(buffer.AsMemory(0, count), timeout.Token);
                    progress?.Report((int)(100 * total / release.Size));
                }
                if (total != release.Size) throw new InvalidDataException("Incomplete update download.");
            }
            await using var file = File.OpenRead(destination);
            if (file.ReadByte() != (release.IsAddon ? 'P' : 'M') || file.ReadByte() != (release.IsAddon ? 'K' : 'Z'))
                throw new InvalidDataException("The update is not the expected file type.");
            if (release.Digest is { Length: > 0 } digest)
            {
                file.Position = 0;
                var actual = "sha256:" + Convert.ToHexString(await SHA256.HashDataAsync(file, timeout.Token));
                if (!actual.Equals(digest, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("The update checksum does not match GitHub's release.");
            }
        }
        catch
        {
            // Never leave a partial download eligible for installation.
            if (created && File.Exists(destination)) File.Delete(destination);
            throw;
        }
    }

    public static string InstalledExecutable()
    {
        var path = Environment.ProcessPath;
        if (!OperatingSystem.IsWindows() || path == null ||
            !Path.GetFileName(path).StartsWith("FrostyBlender", StringComparison.OrdinalIgnoreCase) ||
            File.Exists(Path.Combine(AppContext.BaseDirectory, "FrostyBlender.dll")))
            throw new InvalidOperationException("Automatic installation needs the published single-file Windows app. Download it from Releases.");
        return path;
    }

    public static void StartInstaller(string target, string staged)
    {
        var start = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
            @"WindowsPowerShell\v1.0\powershell.exe")) { UseShellExecute = false, CreateNoWindow = true };
        foreach (var arg in new[] { "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
            Convert.ToBase64String(Encoding.Unicode.GetBytes(InstallerScript(target, staged, Environment.ProcessId,
                Path.Combine(BlenderLink.Folder, "update.log")))) }) start.ArgumentList.Add(arg);
        using var helper = Process.Start(start) ?? throw new IOException("Could not start the update installer.");
        if (helper.WaitForExit(500)) throw new IOException("The update installer exited before the app could close.");
    }

    public static string InstallerScript(string target, string staged, int processId, string log)
    {
        static string Quote(string value) => "'" + value.Replace("'", "''") + "'";
        return $$"""
            $ErrorActionPreference = 'Stop'
            $target = {{Quote(target)}}
            $staged = {{Quote(staged)}}
            $backup = $staged + '.backup'
            $log = {{Quote(log)}}
            $replaced = $false
            function Start-App {
                $start = New-Object System.Diagnostics.ProcessStartInfo
                $start.FileName = $target
                $start.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
                $start.UseShellExecute = $true
                $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
                [Diagnostics.Process]::Start($start) | Out-Null
            }
            try {
                $app = Get-Process -Id {{processId}} -ErrorAction SilentlyContinue
                if ($app -and !$app.WaitForExit(60000)) { throw 'The app did not close within 60 seconds.' }
                for ($attempt = 0; $attempt -lt 30; $attempt++) {
                    try { Move-Item -LiteralPath $target -Destination $backup; break }
                    catch { if ($attempt -eq 29) { throw }; Start-Sleep -Milliseconds 500 }
                }
                Move-Item -LiteralPath $staged -Destination $target
                $replaced = $true
                Start-App
                Remove-Item -LiteralPath $backup -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $log -ErrorAction SilentlyContinue
            } catch {
                $failure = $_.ToString()
                try {
                    if (Test-Path -LiteralPath $backup) {
                        if ($replaced) { Remove-Item -LiteralPath $target }
                        Move-Item -LiteralPath $backup -Destination $target
                        Start-App
                    }
                } catch { $failure += "`nRecovery: $_" }
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($log)) | Out-Null
                [IO.File]::WriteAllText($log, $failure)
            } finally {
                Remove-Item -LiteralPath $staged -ErrorAction SilentlyContinue
            }
            """;
    }
}
