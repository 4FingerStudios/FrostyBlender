using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace FrostyBlender.Services;

/// <summary>The games' own menu art: loading screens for maps, icons for characters, NPCs and cosmetics. The add-on's icons tool finds
/// and decodes them to PNGs (cached on disk); this keeps a key -> PNG map per game so icons seen
/// once show instantly next time.</summary>
public sealed class Icons
{
    private readonly BlenderLink _link;
    private readonly SemaphoreSlim _gate = new(1, 1);  // one tool run at a time
    private readonly Dictionary<string, Dictionary<string, string?>> _known = new();

    public Icons(BlenderLink link) => _link = link;

    /// <summary>Why icons cannot be made with this add-on, or null when they can.</summary>
    public string? Problem
    {
        get
        {
            var info = _link.Info;
            if (info == null) return null;
            if (string.IsNullOrEmpty(info.Reader) || !File.Exists(info.Reader) || !File.Exists(Path.Combine(info.Tools, "icons.py")))
                return "Icons and loading screens need add-on 1.9.1 or newer: install the new frostbite_importer zip in Blender, then reopen Blender.";
            return null;
        }
    }

    private static string Folder(string game) => Path.Combine(BlenderLink.Folder, "icons", game.ToLowerInvariant());
    private static string MapFile(string game) => Path.Combine(Folder(game), "map.json");

    private Dictionary<string, string?> Known(string game)
    {
        if (_known.TryGetValue(game, out var map)) return map;
        map = new Dictionary<string, string?>();
        try
        {
            foreach (var (key, value) in JsonNode.Parse(File.ReadAllText(MapFile(game)))!.AsObject())
                map[key] = value?.GetValue<string>();
        }
        catch (Exception) { /* first run */ }
        return _known[game] = map;
    }

    /// <summary>Icons already found for these keys (null where the game has none).</summary>
    public Dictionary<string, string?> Cached(string game, IEnumerable<string> keys)
    {
        var map = Known(game);
        var result = new Dictionary<string, string?>();
        foreach (var key in keys)
            if (map.TryGetValue(key, out var path) && (path == null || File.Exists(path))) result[key] = path;
        return result;
    }

    /// <summary>Finds and decodes the icons for the requests not cached yet.</summary>
    public async Task<Dictionary<string, string?>> Fetch(string game, IReadOnlyList<JsonObject> portraits, IReadOnlyList<JsonObject> items,
                                                         IReadOnlyList<JsonObject>? maps = null)
    {
        maps ??= Array.Empty<JsonObject>();
        var info = _link.Info;
        var folder = info?.GameFolder(game);
        if (info == null || string.IsNullOrEmpty(info.Reader) || !File.Exists(info.Reader) || !Directory.Exists(folder))
            return new();
        await _gate.WaitAsync();
        try
        {
            var cached = Cached(game, portraits.Concat(items).Concat(maps).Select(r => r["key"]!.GetValue<string>()));
            var todoPortraits = portraits.Where(r => !cached.ContainsKey(r["key"]!.GetValue<string>())).ToList();
            var todoItems = items.Where(r => !cached.ContainsKey(r["key"]!.GetValue<string>())).ToList();
            var todoMaps = maps.Where(r => !cached.ContainsKey(r["key"]!.GetValue<string>())).ToList();
            if (todoPortraits.Count + todoItems.Count + todoMaps.Count == 0) return cached;
            var requests = new JsonObject
            {
                ["portraits"] = new JsonArray(todoPortraits.Select(r => (JsonNode)r.DeepClone()).ToArray()),
                ["items"] = new JsonArray(todoItems.Select(r => (JsonNode)r.DeepClone()).ToArray()),
                ["maps"] = new JsonArray(todoMaps.Select(r => (JsonNode)r.DeepClone()).ToArray()),
            };
            var temp = Path.Combine(Path.GetTempPath(), $"frostyblender_icons_{Guid.NewGuid():N}");
            await File.WriteAllTextAsync(temp + ".req.json", requests.ToJsonString());
            Directory.CreateDirectory(Folder(game));
            var start = new ProcessStartInfo(info.Python) { CreateNoWindow = true, UseShellExecute = false, RedirectStandardError = true, RedirectStandardOutput = true };
            foreach (var arg in new[] { info.Bridge, "icons", "--tools", info.Tools, "--reader", info.Reader, "--game", folder!,
                         "--items", temp + ".req.json", "--icons-dir", Folder(game), "--out", temp + ".out.json" })
                start.ArgumentList.Add(arg);
            try
            {
                using var process = Process.Start(start);
                if (process == null) return cached;
                _ = process.StandardOutput.ReadToEndAsync();
                _ = process.StandardError.ReadToEndAsync();
                await process.WaitForExitAsync();
                var result = JsonNode.Parse(await File.ReadAllTextAsync(temp + ".out.json"))!.AsObject();
                var map = Known(game);
                foreach (var (key, value) in result["icons"]?.AsObject() ?? new JsonObject())
                    cached[key] = map[key] = value?.GetValue<string>();
                await File.WriteAllTextAsync(MapFile(game), new JsonObject(map.Select(kv =>
                    new KeyValuePair<string, JsonNode?>(kv.Key, kv.Value == null ? null : JsonValue.Create(kv.Value)))).ToJsonString());
            }
            catch (Exception)
            {
                // Icons are a nicety: tiles keep their initials.
            }
            finally
            {
                try { File.Delete(temp + ".req.json"); File.Delete(temp + ".out.json"); } catch (IOException) { }
            }
            return cached;
        }
        finally
        {
            _gate.Release();
        }
    }
}
