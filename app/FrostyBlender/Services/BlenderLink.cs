using System;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace FrostyBlender.Services;

/// <summary>What the Blender add-on writes to %LOCALAPPDATA%\FrostyBlender\link.json: Blender's Python,
/// the add-on's tools and bridge, the game folders and the local port. It never holds the BFN key.</summary>
public sealed record LinkInfo(
    string AddonVersion, int Port, string Blender, string BlenderVersion, string Python, string Bridge,
    string Tools, string Reader, string Work, string Gw1, string Gw2, string Bfn, bool BfnReady, DateTime Updated)
{
    public string GameFolder(string game) => game switch { "GW1" => Gw1, "GW2" => Gw2, "BFN" => Bfn, _ => "" };
}

/// <summary>The add-on's local server: ping, status, armatures and import requests.</summary>
public sealed class BlenderLink
{
    public static string Folder { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "FrostyBlender");
    public static string LinkFile => Path.Combine(Folder, "link.json");

    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(4) };

    public LinkInfo? Info { get; private set; }

    /// <summary>Reads link.json; false when Blender has never run with the add-on.</summary>
    public bool Load()
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(LinkFile))!.AsObject();
            var games = node["games"]?.AsObject();
            Info = new LinkInfo(
                Str(node, "addon_version"), node["port"]?.GetValue<int>() ?? 24567, Str(node, "blender"),
                Str(node, "blender_version"), Str(node, "python"), Str(node, "bridge"), Str(node, "tools"),
                Str(node, "reader"), Str(node, "work"), Str(games, "GW1"), Str(games, "GW2"), Str(games, "BFN"),
                node["bfn_ready"]?.GetValue<bool>() ?? false,
                DateTimeOffset.FromUnixTimeSeconds((long)(node["updated"]?.GetValue<double>() ?? 0)).LocalDateTime);
            return true;
        }
        catch (Exception)
        {
            Info = null;
            return false;
        }
    }

    private static string Str(JsonObject? node, string key) => node?[key]?.GetValue<string>() ?? "";

    private HttpRequestMessage Request(HttpMethod method, string path)
    {
        var request = new HttpRequestMessage(method, $"http://127.0.0.1:{Info?.Port ?? 24567}{path}");
        request.Headers.Add("X-FrostyBlender", "1");
        return request;
    }

    public async Task<JsonObject?> Get(string path, CancellationToken token = default)
    {
        try
        {
            using var response = await _http.SendAsync(Request(HttpMethod.Get, path), token);
            if (!response.IsSuccessStatusCode) return null;
            return JsonNode.Parse(await response.Content.ReadAsStringAsync(token))?.AsObject();
        }
        catch (Exception)
        {
            return null;  // Blender closed, or the link is off
        }
    }

    /// <summary>Sends an import; returns the job id, or throws with the reason.</summary>
    public Task<string> Import(JsonObject body) => Submit("/import", body);

    public Task<string> UpdateAddon(JsonObject body) => Submit("/addon-update", body);

    private async Task<string> Submit(string path, JsonObject body)
    {
        using var request = Request(HttpMethod.Post, path);
        request.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        HttpResponseMessage response;
        try
        {
            response = await _http.SendAsync(request);
        }
        catch (Exception)
        {
            throw new InvalidOperationException("Blender isn't running with the FrostyBlender add-on. Open Blender and try again.");
        }
        using var ownedResponse = response;
        var text = await response.Content.ReadAsStringAsync();
        var node = JsonNode.Parse(text)?.AsObject();
        if (!response.IsSuccessStatusCode || node?["ok"]?.GetValue<bool>() != true)
            throw new InvalidOperationException(node?["error"]?.GetValue<string>() ?? $"Blender answered {(int)response.StatusCode}");
        return node!["job"]!.GetValue<string>();
    }

    public static readonly JsonSerializerOptions Pretty = new() { WriteIndented = true };
}
