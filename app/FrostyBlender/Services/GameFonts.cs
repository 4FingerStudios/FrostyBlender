using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Media;
using Avalonia.Media.Fonts;
using Avalonia.Platform;

namespace FrostyBlender.Services;

/// <summary>The games' own UI fonts: Garden Warfare 2's MonsterFonts House of Terror for headings and Cafeteria for
/// text. They are copied out of the player's GW2 install with the add-on's explorer tool the first time Blender is
/// linked, so nothing from the games ships with the app. Until then the app uses Lilita One and Inter.</summary>
public static class GameFonts
{
    private static readonly (string Asset, string File, string Family)[] Wanted =
    {
        ("ui/font/charset/monsterfonts-houseofterror", "headings.ttf", "MonsterFonts-HouseofTerror"),
        ("ui/font/charset/cafeteria-bold_proper", "text.ttf", "Cafeteria"),
    };
    private static readonly Uri Key = new("fonts:FrostyBlenderGame", UriKind.Absolute);
    private const string DisplayFallback = "avares://FrostyBlender/Assets/Fonts#Lilita One";
    private static bool _applied, _tried;

    public static string Folder => Path.Combine(BlenderLink.Folder, "fonts");
    private const string Version = "3";  // bump to copy the fonts again (e.g. after changing Enlarge)
    public static bool Ready => Wanted.All(w => File.Exists(Path.Combine(Folder, w.File)))
                                && File.Exists(Path.Combine(Folder, "version.txt")) && File.ReadAllText(Path.Combine(Folder, "version.txt")) == Version;

    /// <summary>Both faces are narrow: drawn at the app's text sizes they read small. Lowering a font's units per em
    /// (the 'head' table) draws every glyph, and the line height, larger by the factor.</summary>
    private static void Enlarge(string path, double factor)
    {
        var data = File.ReadAllBytes(path);
        int tables = data[4] << 8 | data[5];
        for (var i = 0; i < tables; i++)
        {
            var record = 12 + 16 * i;
            if (System.Text.Encoding.ASCII.GetString(data, record, 4) != "head") continue;
            var head = data[record + 8] << 24 | data[record + 9] << 16 | data[record + 10] << 8 | data[record + 11];
            var upem = data[head + 18] << 8 | data[head + 19];
            var scaled = (int)Math.Round(upem / factor);
            if (scaled < 16) return;
            data[head + 18] = (byte)(scaled >> 8);
            data[head + 19] = (byte)scaled;
            File.WriteAllBytes(path, data);
            return;
        }
    }

    /// <summary>Points the Display and Body font resources at the game fonts, when they have been copied.</summary>
    public static void Apply()
    {
        if (_applied || !Ready || Application.Current is not { } app) return;
        try
        {
            FontManager.Current.AddFontCollection(new FileFontCollection(Key, Wanted.Select(w => Path.Combine(Folder, w.File))));
            app.Resources["Display"] = new FontFamily($"{Key}#{Wanted[0].Family}, {DisplayFallback}");
            app.Resources["Body"] = new FontFamily($"{Key}#{Wanted[1].Family}, fonts:Inter#Inter, $Default");
            _applied = true;
        }
        catch (Exception error)
        {
            // A font the system can't read: keep the bundled ones.
            try { File.WriteAllText(Path.Combine(Folder, "error.txt"), error.ToString()); } catch (IOException) { }
        }
    }

    /// <summary>Fonts read from files on disk (Avalonia's own collections read only bundled assets).</summary>
    private sealed class FileFontCollection : FontCollectionBase
    {
        private readonly Uri _key;
        private readonly string[] _files;
        private readonly List<IGlyphTypeface> _faces = new();

        public FileFontCollection(Uri key, IEnumerable<string> files) => (_key, _files) = (key, files.ToArray());

        public override Uri Key => _key;
        public override int Count => _faces.Count;
        public override FontFamily this[int index] => new(_key, _faces[index].FamilyName);
        public override IEnumerator<FontFamily> GetEnumerator() => _faces.Select(f => new FontFamily(_key, f.FamilyName)).GetEnumerator();

        public override void Initialize(IFontManagerImpl fontManager)
        {
            // The stream overload is public at run time but hidden from compilation (Avalonia private API).
            var create = typeof(IFontManagerImpl).GetMethod("TryCreateGlyphTypeface",
                new[] { typeof(Stream), typeof(FontSimulations), typeof(IGlyphTypeface).MakeByRefType() });
            if (create == null) return;
            foreach (var file in _files)
            {
                var args = new object?[] { new MemoryStream(File.ReadAllBytes(file)), FontSimulations.None, null };
                if (create.Invoke(fontManager, args) is true && args[2] is IGlyphTypeface face) _faces.Add(face);
            }
        }

        public override bool TryGetGlyphTypeface(string familyName, FontStyle style, FontWeight weight, FontStretch stretch,
                                                 [NotNullWhen(true)] out IGlyphTypeface? glyphTypeface)
        {
            glyphTypeface = _faces.FirstOrDefault(f => string.Equals(f.FamilyName, familyName, StringComparison.OrdinalIgnoreCase));
            return glyphTypeface != null;
        }
    }

    /// <summary>Copies the fonts out of the GW2 install once. True when they are ready to apply.</summary>
    public static async Task<bool> Extract(LinkInfo? info)
    {
        if (Ready) return true;
        var game = info?.GameFolder("GW2");
        if (_tried || info == null || string.IsNullOrEmpty(info.Python) || !Directory.Exists(game)) return false;
        _tried = true;
        var temp = Path.Combine(Path.GetTempPath(), $"frostyblender_fonts_{Guid.NewGuid():N}");
        try
        {
            Directory.CreateDirectory(temp);
            await File.WriteAllTextAsync(Path.Combine(temp, "items.json"),
                new JsonArray(Wanted.Select(w => (JsonNode)JsonValue.Create(w.Asset)!).ToArray()).ToJsonString());
            var start = new ProcessStartInfo(info.Python) { CreateNoWindow = true, UseShellExecute = false, RedirectStandardError = true, RedirectStandardOutput = true };
            foreach (var arg in new[] { info.Bridge, "explorer-export", "--tools", info.Tools, "--reader", info.Reader ?? "", "--game", game!,
                         "--items", Path.Combine(temp, "items.json"), "--dest", Path.Combine(temp, "out"), "--out", Path.Combine(temp, "result.json") })
                start.ArgumentList.Add(arg);
            using var process = Process.Start(start);
            if (process == null) return false;
            _ = process.StandardOutput.ReadToEndAsync();
            _ = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            Directory.CreateDirectory(Folder);
            foreach (var (asset, file, _) in Wanted)
            {
                var found = Path.Combine(temp, "out", asset.Replace('/', Path.DirectorySeparatorChar) + ".res");
                if (!File.Exists(found)) return false;
                var head = new byte[4];
                using (var stream = File.OpenRead(found)) stream.ReadExactly(head);
                if (!(head.SequenceEqual(new byte[] { 0, 1, 0, 0 }) || head.SequenceEqual("OTTO"u8.ToArray()))) return false;  // TrueType or OpenType
                File.Copy(found, Path.Combine(Folder, file), true);
            }
            Enlarge(Path.Combine(Folder, Wanted[0].File), 1.15);
            Enlarge(Path.Combine(Folder, Wanted[1].File), 1.4);
            File.WriteAllText(Path.Combine(Folder, "version.txt"), Version);
            return Ready;
        }
        catch (Exception)
        {
            return false;  // the fonts are a nicety
        }
        finally
        {
            try { Directory.Delete(temp, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }
}
