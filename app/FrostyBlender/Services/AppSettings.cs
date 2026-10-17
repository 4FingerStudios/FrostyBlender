using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace FrostyBlender.Services;

/// <summary>The signed-in Discord account: only what the app shows (never a token).</summary>
public sealed class DiscordUser
{
    public string Id { get; set; } = "";
    public string Username { get; set; } = "";
    public string GlobalName { get; set; } = "";
    public string Avatar { get; set; } = "";

    [JsonIgnore] public string DisplayName => GlobalName.Length > 0 ? GlobalName : Username;
    [JsonIgnore] public string AvatarUrl => Avatar.Length > 0
        ? $"https://cdn.discordapp.com/avatars/{Id}/{Avatar}.png?size=128"
        : "https://cdn.discordapp.com/embed/avatars/0.png";
}

/// <summary>App preferences in %LOCALAPPDATA%\FrostyBlender\settings.json.</summary>
public sealed class AppSettings
{
    public bool DiscordPresence { get; set; } = true;
    public DiscordUser? DiscordUser { get; set; }

    private static string File => Path.Combine(BlenderLink.Folder, "settings.json");

    public static AppSettings Load()
    {
        try { return JsonSerializer.Deserialize<AppSettings>(System.IO.File.ReadAllText(File)) ?? new AppSettings(); }
        catch (Exception) { return new AppSettings(); }
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(BlenderLink.Folder);
            System.IO.File.WriteAllText(File, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (IOException) { /* preferences are a convenience */ }
    }
}
