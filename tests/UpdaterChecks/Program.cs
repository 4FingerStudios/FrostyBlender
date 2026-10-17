using System.Diagnostics;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using FrostyBlender.Services;

var count = 0;
void Check(bool condition, string name)
{
    if (!condition) throw new Exception(name);
    Console.WriteLine("PASS " + name);
    count++;
}
async Task Reject(Func<Task> action, string name)
{
    try { await action(); }
    catch { Check(true, name); return; }
    throw new Exception("Expected rejection: " + name);
}
string Release(string name = "FrostyBlender-1.4.0-windows-x64.exe", string? url = null) => JsonSerializer.Serialize(new
{
    tag_name = "v1.10.1", draft = false, prerelease = false,
    assets = new object[] {
        new { name = "frostbite_importer-1.10.1-windows-x64.zip", size = 100, browser_download_url = "https://github.com/4FingerStudios/FrostyBlender/releases/download/v1.10.1/frostbite_importer-1.10.1-windows-x64.zip" },
        new { name, size = 4, browser_download_url = url ?? "https://github.com/4FingerStudios/FrostyBlender/releases/download/v1.10.1/" + name }
    }
});
var release = AppUpdater.ParseRelease(Release());
Check(release.Version == new Version(1, 4, 0), "Uses app asset version, not add-on tag");
Check(!(release.Version > new Version(1, 4, 0)), "Does not reinstall equal version");
Check(release.Version > new Version(1, 3, 0), "Detects newer app");
Check(!(release.Version > new Version(1, 5, 0)), "Does not downgrade app");
var addon = AppUpdater.ParseRelease(Release(), true);
Check(addon.IsAddon && addon.Version == new Version(1, 10, 1), "Selects add-on ZIP independently of desktop app");
await Reject(() => Task.FromResult(AppUpdater.ParseRelease(Release("frostbite_importer-1.10.1-windows-x64.zip"))), "Rejects release without app");
await Reject(() => Task.FromResult(AppUpdater.ParseRelease(Release(url: "https://example.com/app.exe"))), "Rejects foreign download host");
await Reject(() => Task.FromResult(AppUpdater.ParseRelease(Release().Replace("\"prerelease\":false", "\"prerelease\":true"))), "Rejects prerelease");
var handler = new FakeHttp();
var updater = new AppUpdater(new HttpClient(handler));
handler.Body = Encoding.UTF8.GetBytes(Release());
Check((await updater.CheckAsync()).Version == release.Version, "Checks GitHub release API");
Check(handler.Url == "https://api.github.com/repos/4FingerStudios/FrostyBlender/releases/latest" && handler.UserAgent, "Sends latest endpoint and User-Agent");
handler.Status = HttpStatusCode.Forbidden;
await Reject(async () => await updater.CheckAsync(), "Handles API rate limit");
handler.Status = HttpStatusCode.OK;
var folder = Path.Combine(Path.GetTempPath(), "Frosty updater ' [$] " + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(folder);
try
{
    var download = Path.Combine(folder, "download.exe");
    handler.Body = new byte[] { 77, 90, 1, 2 };
    var digest = "sha256:" + Convert.ToHexString(SHA256.HashData(handler.Body));
    await updater.DownloadAsync(release with { Digest = digest }, download);
    Check(File.ReadAllBytes(download).SequenceEqual(handler.Body), "Verifies and saves valid download");
    await Reject(() => updater.DownloadAsync(release, download), "Refuses existing destination");
    Check(File.Exists(download), "Preserves existing destination");
    File.Delete(download);
    handler.Body = new byte[] { 80, 75, 1, 2 };
    await updater.DownloadAsync(addon with { Size = 4 }, download);
    Check(File.ReadAllBytes(download).SequenceEqual(handler.Body), "Accepts ZIP download for add-on");
    File.Delete(download);
    handler.Body = new byte[] { 77, 90, 1, 2 };
    await Reject(() => updater.DownloadAsync(release with { Digest = "sha256:wrong" }, download), "Rejects checksum mismatch");
    Check(!File.Exists(download), "Removes corrupt download");
    handler.Body = new byte[] { 77, 90 };
    await Reject(() => updater.DownloadAsync(release, download), "Rejects truncated download");
    handler.Body = new byte[] { 77, 90, 1, 2, 3 };
    await Reject(() => updater.DownloadAsync(release, download), "Rejects oversized download");
    handler.Body = Encoding.UTF8.GetBytes("html");
    await Reject(() => updater.DownloadAsync(release, download), "Rejects non-executable download");
    if (args.Contains("--live"))
    {
        var live = new AppUpdater();
        var latest = await live.CheckAsync();
        await live.DownloadAsync(latest, download);
        Check(File.Exists(download), $"Downloads and verifies real GitHub app {latest.Version}");
        File.Delete(download);
        var latestAddon = await live.CheckAddonAsync();
        await live.DownloadAsync(latestAddon, download);
        Check(File.Exists(download), $"Downloads and verifies real GitHub add-on {latestAddon.Version}");
        File.Delete(download);
    }
    if (OperatingSystem.IsWindows())
    {
        var target = Path.Combine(folder, "FrostyBlender.exe");
        var staged = Path.Combine(folder, "staged.exe");
        var log = Path.Combine(folder, "update.log");
        var system = Environment.GetFolderPath(Environment.SpecialFolder.System);
        File.Copy(Path.Combine(system, "where.exe"), target);
        var oldHash = SHA256.HashData(File.ReadAllBytes(target));
        File.Copy(Path.Combine(system, "whoami.exe"), staged);
        var newHash = SHA256.HashData(File.ReadAllBytes(staged));
        using var sleeper = StartPowerShell("Start-Sleep -Seconds 3");
        using var installer = StartPowerShell(AppUpdater.InstallerScript(target, staged, sleeper.Id, log));
        await Task.Delay(500);
        Check(SHA256.HashData(File.ReadAllBytes(target)).SequenceEqual(oldHash), "Installer waits for old process to exit");
        await installer.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(30));
        Check(!File.Exists(log), "Installation succeeds with quoted and special path characters: " + (File.Exists(log) ? File.ReadAllText(log) : "OK"));
        Check(SHA256.HashData(File.ReadAllBytes(target)).SequenceEqual(newHash), "Replaces executable");
        Check(!File.Exists(staged) && !File.Exists(staged + ".backup"), "Cleans stage and backup on success");
        await File.WriteAllTextAsync(staged, "Not an executable");
        using var failed = StartPowerShell(AppUpdater.InstallerScript(target, staged, int.MaxValue, log));
        await failed.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(30));
        Check(File.Exists(log), "Logs failed restart");
        Check(SHA256.HashData(File.ReadAllBytes(target)).SequenceEqual(newHash), "Restores previous executable on failure");
    }
}
finally
{
    // The restarted test executable may still be exiting when the installer returns.
    for (var attempt = 0; ; attempt++)
    {
        try { Directory.Delete(folder, true); break; }
        catch (UnauthorizedAccessException) when (attempt < 20) { await Task.Delay(250); }
        catch (IOException) when (attempt < 20) { await Task.Delay(250); }
    }
}
Console.WriteLine($"{count} updater checks passed.");

static Process StartPowerShell(string script)
{
    var start = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
        @"WindowsPowerShell\v1.0\powershell.exe")) { UseShellExecute = false, CreateNoWindow = true };
    foreach (var arg in new[] { "-NoProfile", "-NonInteractive", "-EncodedCommand", Convert.ToBase64String(Encoding.Unicode.GetBytes(script)) })
        start.ArgumentList.Add(arg);
    return Process.Start(start)!;
}

sealed class FakeHttp : HttpMessageHandler
{
    public byte[] Body = Array.Empty<byte>();
    public HttpStatusCode Status = HttpStatusCode.OK;
    public string? Url;
    public bool UserAgent;
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
    {
        Url = request.RequestUri?.ToString();
        UserAgent = request.Headers.UserAgent.Count > 0;
        return Task.FromResult(new HttpResponseMessage(Status) { Content = new ByteArrayContent(Body) });
    }
}

namespace FrostyBlender.Services
{
    static class BlenderLink { public static string Folder => Path.GetTempPath(); }
}
