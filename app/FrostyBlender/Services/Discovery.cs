using System;
using System.Diagnostics;
using System.IO;
using System.Text.Json.Nodes;
using System.Threading.Tasks;

namespace FrostyBlender.Services;

/// <summary>Lists a game's maps, characters, NPCs, props and animations by running the add-on's own
/// discover tools on Blender's Python (so Blender does not need to be open). Results are cached per
/// game and add-on version; a refresh scans again.</summary>
public sealed class Discovery
{
    private readonly BlenderLink _link;

    public Discovery(BlenderLink link) => _link = link;

    public static string BridgeAction(Section section) => section switch
    {
        Section.Maps => "discover",
        Section.Characters => "cosmetics-discover",
        Section.Npcs => "npcs-discover",
        Section.Props => "props-discover",
        Section.Animations => "animations-discover",
        _ => throw new ArgumentOutOfRangeException(nameof(section)),
    };

    private string CacheFile(Section section, string game) =>
        Path.Combine(BlenderLink.Folder, "cache", $"{game}_{section}_{_link.Info?.AddonVersion}.json".ToLowerInvariant());

    public async Task<JsonObject> Scan(Section section, string game, bool refresh)
    {
        var info = _link.Info ?? throw new InvalidOperationException(
            "Open Blender once with the FrostyBlender add-on installed, then click Retry.");
        var folder = info.GameFolder(game);
        if (string.IsNullOrEmpty(folder) || !Directory.Exists(folder))
            throw new InvalidOperationException(
                $"The {game} folder isn't set. Set it in Blender: Edit > Preferences > Add-ons > Frostbite Map Importer.");
        var cache = CacheFile(section, game);
        if (!refresh && File.Exists(cache))
        {
            try { return JsonNode.Parse(await File.ReadAllTextAsync(cache))!.AsObject(); }
            catch (Exception) { /* scan again */ }
        }
        Directory.CreateDirectory(Path.GetDirectoryName(cache)!);
        var output = Path.Combine(Path.GetTempPath(), $"frostyblender_{Guid.NewGuid():N}.json");
        var start = new ProcessStartInfo(info.Python)
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
        };
        foreach (var arg in new[] { info.Bridge, BridgeAction(section), "--tools", info.Tools, "--game", folder, "--out", output })
            start.ArgumentList.Add(arg);
        using var process = Process.Start(start) ?? throw new InvalidOperationException("Couldn't start Blender's Python");
        var stderr = process.StandardError.ReadToEndAsync();
        _ = process.StandardOutput.ReadToEndAsync();
        await process.WaitForExitAsync();
        JsonObject result;
        try
        {
            result = JsonNode.Parse(await File.ReadAllTextAsync(output))!.AsObject();
        }
        catch (Exception)
        {
            var lines = (await stderr).Trim().Split('\n');
            throw new InvalidOperationException("Scan failed: " + (lines.Length > 0 ? lines[^1].Trim() : "no output"));
        }
        finally
        {
            try { File.Delete(output); } catch (IOException) { }
        }
        if (result["ok"]?.GetValue<bool>() != true)
            throw new InvalidOperationException("Scan failed: " + (result["error"]?.GetValue<string>() ?? "unknown error"));
        await File.WriteAllTextAsync(cache, result.ToJsonString());
        return result;
    }
}

public enum Section { Maps, Characters, Npcs, Props, Animations, Settings }
