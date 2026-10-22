using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace FrostyBlender.ViewModels;

/// <summary>A game sound (SoundWaveAsset) in the explorer's Sounds list.</summary>
public sealed partial class SoundRow : ObservableObject
{
    public required string Path { get; init; }
    public string Kind { get; init; } = "";
    public double Duration { get; init; }
    public int Channels { get; init; }
    public string Codec { get; init; } = "";
    public int Rate { get; init; }
    public int Variations { get; init; }
    [ObservableProperty] private bool _checked;

    public string Name => Path[(Path.LastIndexOf('/') + 1)..];
    public string Folder => Path.LastIndexOf('/') is var cut and > 0 ? Path[..cut] : "";
    public string Glyph => Kind switch { "Voice" => "", "Music" => "", _ => "" };
    public string DurationText => Duration >= 60 ? $"{(int)Duration / 60}:{Duration % 60:00.0}" : $"{Duration:0.0} s";
    public string Info => $"{(Channels == 1 ? "Mono" : Channels == 2 ? "Stereo" : $"{Channels} ch")} · {Codec}" +
        (Rate > 0 ? $" · {Rate / 1000.0:0.#} kHz" : "") + (Variations > 1 ? $" · {Variations} takes" : "");
}

/// <summary>A UI string in the explorer's Text list.</summary>
public sealed record TextRow(string Hash, string Id, string Text)
{
    public string Key => Id.Length > 0 ? Id : "#" + Hash;
    public string OneLine => Text.Replace("\r", "").Replace('\n', ' ');
}

public sealed record LanguageOption(string Code, string Name)
{
    public override string ToString() => Name;
}

public partial class MainViewModel
{
    public IReadOnlyList<string> ExplorerModes { get; } = new[] { "Files", "Sounds", "Text" };
    public IReadOnlyList<string> SoundKinds { get; } = new[] { "All", "Voice", "Music", "Effect" };

    [ObservableProperty] private string _explorerMode = "Files";
    public bool ExplorerIsFiles => ExplorerMode == "Files";
    public bool ExplorerIsSounds => ExplorerMode == "Sounds";
    public bool ExplorerIsText => ExplorerMode == "Text";

    partial void OnExplorerModeChanged(string value)
    {
        OnPropertyChanged(nameof(ExplorerIsFiles));
        OnPropertyChanged(nameof(ExplorerIsSounds));
        OnPropertyChanged(nameof(ExplorerIsText));
        _ = LoadExplorerMedia(false);
    }

    /// <summary>Loads the open mode's list for the current game (the Files list loads in LoadExplorer).</summary>
    private Task LoadExplorerMedia(bool refresh) => ExplorerMode switch
    {
        "Sounds" => LoadSounds(refresh),
        "Text" => LoadText(refresh, TextLanguage?.Code),
        _ => Task.CompletedTask,
    };

    // ---------------- sounds ----------------
    private List<SoundRow> _soundsAll = new();
    private string _soundsGame = "";
    private int _soundsScan;
    private readonly Dictionary<string, SoundRow> _soundTicks = new();

    [ObservableProperty] private ObservableCollection<SoundRow> _soundRows = new();
    [ObservableProperty] private string _soundSearch = "";
    [ObservableProperty] private string _soundKind = "All";
    [ObservableProperty] private SoundRow? _soundSelected;
    [ObservableProperty] private bool _soundsLoading;
    [ObservableProperty] private string _soundsError = "";
    [ObservableProperty] private string _soundsCount = "";
    [ObservableProperty] private string _soundsNote = "";
    [ObservableProperty] private int _soundsChecked;

    public bool SoundsHasError => SoundsError.Length > 0;
    public bool SoundHasSelection => SoundSelected != null;
    public string SoundExportText => SoundsChecked > 0 ? $"Export {SoundsChecked} sound{(SoundsChecked == 1 ? "" : "s")} as WAV"
        : SoundSelected != null ? "Export this sound as WAV" : "Export as WAV";

    partial void OnSoundsErrorChanged(string value) => OnPropertyChanged(nameof(SoundsHasError));
    partial void OnSoundSearchChanged(string value) => ShowSounds();
    partial void OnSoundKindChanged(string value) => ShowSounds();
    partial void OnSoundsCheckedChanged(int value) => OnPropertyChanged(nameof(SoundExportText));
    partial void OnSoundSelectedChanged(SoundRow? value)
    {
        OnPropertyChanged(nameof(SoundHasSelection));
        OnPropertyChanged(nameof(SoundExportText));
    }

    private async Task LoadSounds(bool refresh)
    {
        if (!refresh && _soundsGame == Game && _soundsAll.Count > 0) return;
        var id = ++_soundsScan;
        SoundsLoading = true;
        SoundsError = "";
        SoundRows = new();
        _soundTicks.Clear();
        SoundsChecked = 0;
        try
        {
            if (!LinkLoaded)
                throw new InvalidOperationException("Install the Frostbite Map Importer add-on in Blender and open Blender once.");
            var result = await _discovery.Run("explorer-sounds", Game);
            if (id != _soundsScan) return;
            var rows = new List<SoundRow>();
            foreach (var node in result["sounds"]?.AsArray() ?? new JsonArray())
            {
                var row = new SoundRow
                {
                    Path = node!["name"]!.GetValue<string>(),
                    Kind = node["kind"]?.GetValue<string>() ?? "",
                    Duration = node["duration"]?.GetValue<double>() ?? 0,
                    Channels = node["channels"]?.GetValue<int>() ?? 0,
                    Codec = node["codec"]?.GetValue<string>() ?? "",
                    Rate = node["rate"]?.GetValue<int>() ?? 0,
                    Variations = node["variations"]?.GetValue<int>() ?? 1,
                };
                row.PropertyChanged += (_, e) =>
                {
                    if (e.PropertyName != nameof(SoundRow.Checked)) return;
                    if (row.Checked) _soundTicks[row.Path] = row; else _soundTicks.Remove(row.Path);
                    SoundsChecked = _soundTicks.Count;
                };
                rows.Add(row);
            }
            _soundsAll = rows;
            _soundsGame = Game;
            SoundsCount = $"{rows.Count:N0} sounds";
            // Say what can't be decoded here, and what would decode it.
            var decoder = result["decoder"]?.GetValue<string>() ?? "";
            var foreign = rows.Where(r => r.Codec is not ("EA-XAS" or "PCM" or "")).GroupBy(r => r.Codec).ToList();
            SoundsNote = foreign.Count == 0 ? "Every sound here decodes to WAV inside FrostyBlender."
                : decoder.Length > 0 ? $"{foreign.Sum(g => g.Count()):N0} sounds ({string.Join(", ", foreign.Select(g => g.Key))}) decode through vgmstream at {decoder}."
                : $"{foreign.Sum(g => g.Count()):N0} sounds use {string.Join(", ", foreign.Select(g => g.Key))}, which needs vgmstream: put vgmstream-cli.exe " +
                  "(from vgmstream.org) in Documents\\Frosty Blender\\vgmstream or on PATH. The rest export as is.";
            ShowSounds();
        }
        catch (Exception error)
        {
            if (id == _soundsScan) SoundsError = error.Message;
        }
        finally
        {
            if (id == _soundsScan) SoundsLoading = false;
        }
    }

    private void ShowSounds()
    {
        var words = SoundSearch.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
        SoundRows = new ObservableCollection<SoundRow>(_soundsAll.Where(r =>
            (SoundKind == "All" || r.Kind == SoundKind) && words.All(w => r.Path.Contains(w, StringComparison.OrdinalIgnoreCase))));
    }

    private List<SoundRow> SoundsChosen()
    {
        var chosen = _soundTicks.Values.ToList();
        if (chosen.Count == 0 && SoundSelected != null) chosen.Add(SoundSelected);
        return chosen;
    }

    [RelayCommand]
    private Task SoundsRefresh() => LoadSounds(true);

    [RelayCommand]
    private void SoundTickAll()
    {
        var tick = SoundRows.Any(r => !r.Checked);
        foreach (var row in SoundRows) row.Checked = tick;
    }

    [RelayCommand]
    private void SoundClear()
    {
        foreach (var row in _soundTicks.Values.ToList()) row.Checked = false;
        _soundTicks.Clear();
        SoundsChecked = 0;
    }

    private async Task<JsonObject> ExportSounds(IEnumerable<SoundRow> rows, string dest)
    {
        var items = Path.Combine(Path.GetTempPath(), $"frostyblender_items_{Guid.NewGuid():N}.json");
        try
        {
            Directory.CreateDirectory(dest);
            await File.WriteAllTextAsync(items, new JsonArray(rows.Select(r => (JsonNode)r.Path).ToArray()).ToJsonString());
            return await _discovery.Run("explorer-sound-export", Game, "--items", items, "--dest", dest);
        }
        finally
        {
            try { File.Delete(items); } catch (IOException) { }
        }
    }

    [RelayCommand]
    private async Task SoundExport()
    {
        var chosen = SoundsChosen();
        if (chosen.Count == 0 || ExplorerBusy)
        {
            ExplorerStatus = "Tick sounds (or click one) to export.";
            return;
        }
        ExplorerBusy = true;
        ExplorerStatus = $"Exporting {chosen.Count} sound{(chosen.Count == 1 ? "" : "s")}…";
        try
        {
            var result = await ExportSounds(chosen, ExplorerDest);
            var written = result["written"]?.AsArray().Count ?? 0;
            var errors = result["errors"]?.AsArray() ?? new JsonArray();
            ExplorerStatus = $"Saved {written} WAV file{(written == 1 ? "" : "s")} to {ExplorerDest}" +
                (errors.Count > 0 ? $". {errors.Count} couldn't be exported: {errors[0]?["name"]} ({errors[0]?["error"]})" : ".");
        }
        catch (Exception error)
        {
            ExplorerStatus = "Export failed: " + error.Message;
        }
        finally
        {
            ExplorerBusy = false;
        }
    }

    /// <summary>Decodes the selected sound to a scratch folder and opens it in the system's player.</summary>
    [RelayCommand]
    private async Task SoundPlay()
    {
        if (SoundSelected is not { } row || ExplorerBusy) return;
        ExplorerBusy = true;
        ExplorerStatus = $"Decoding {row.Name}…";
        try
        {
            var folder = Path.Combine(Path.GetTempPath(), "frostyblender_sounds");
            var result = await ExportSounds(new[] { row }, folder);
            var wav = result["written"]?.AsArray().FirstOrDefault()?.GetValue<string>();
            if (wav == null)
            {
                var errors = result["errors"]?.AsArray();
                ExplorerStatus = "Couldn't play it: " + (errors?.FirstOrDefault()?["error"]?.GetValue<string>() ?? "nothing decoded");
                return;
            }
            Open(Path.GetFullPath(wav, folder));
            ExplorerStatus = row.Variations > 1 ? $"Playing take 1 of {row.Variations}. Export to get every take." : $"Playing {row.Name}.";
        }
        catch (Exception error)
        {
            ExplorerStatus = "Couldn't play it: " + error.Message;
        }
        finally
        {
            ExplorerBusy = false;
        }
    }

    // ---------------- text ----------------
    private List<TextRow> _textAll = new();
    private string _textGame = "";
    private int _textScan;

    [ObservableProperty] private ObservableCollection<TextRow> _textRows = new();
    [ObservableProperty] private ObservableCollection<LanguageOption> _textLanguages = new();
    [ObservableProperty] private LanguageOption? _textLanguage;
    [ObservableProperty] private string _textSearch = "";
    [ObservableProperty] private TextRow? _textSelected;
    [ObservableProperty] private bool _textLoading;
    [ObservableProperty] private string _textError = "";
    [ObservableProperty] private string _textCount = "";
    [ObservableProperty] private string _textShown = "";
    private bool _textSwitching;

    public bool TextHasError => TextError.Length > 0;
    public bool TextHasSelection => TextSelected != null;

    partial void OnTextErrorChanged(string value) => OnPropertyChanged(nameof(TextHasError));
    partial void OnTextSearchChanged(string value) => ShowText();
    partial void OnTextSelectedChanged(TextRow? value) => OnPropertyChanged(nameof(TextHasSelection));
    partial void OnTextLanguageChanged(LanguageOption? value)
    {
        if (!_textSwitching && value != null) _ = LoadText(true, value.Code);
    }

    private async Task LoadText(bool refresh, string? language)
    {
        if (!refresh && _textGame == Game && _textAll.Count > 0) return;
        var id = ++_textScan;
        TextLoading = true;
        TextError = "";
        try
        {
            if (!LinkLoaded)
                throw new InvalidOperationException("Install the Frostbite Map Importer add-on in Blender and open Blender once.");
            var extra = language != null && _textGame == Game ? new[] { "--language", language } : Array.Empty<string>();
            var result = await _discovery.Run("explorer-text", Game, extra);
            if (id != _textScan) return;
            var rows = new List<TextRow>();
            foreach (var node in result["strings"]?.AsArray() ?? new JsonArray())
            {
                var row = node!.AsArray();
                rows.Add(new TextRow(row[0]!.GetValue<string>(), row[1]!.GetValue<string>(), row[2]!.GetValue<string>()));
            }
            _textAll = rows;
            _textGame = Game;
            var code = result["language"]?.GetValue<string>() ?? "";
            _textSwitching = true;
            TextLanguages = new ObservableCollection<LanguageOption>((result["languages"]?.AsArray() ?? new JsonArray())
                .Select(l => new LanguageOption(l![0]!.GetValue<string>(), l[1]!.GetValue<string>())));
            TextLanguage = TextLanguages.FirstOrDefault(l => l.Code == code);
            _textSwitching = false;
            TextCount = $"{rows.Count:N0} strings · {rows.Count(r => r.Id.Length > 0):N0} named";
            ShowText();
        }
        catch (Exception error)
        {
            if (id == _textScan) TextError = error.Message;
        }
        finally
        {
            _textSwitching = false;
            if (id == _textScan) TextLoading = false;
        }
    }

    private void ShowText()
    {
        var words = TextSearch.Trim().Split(' ', StringSplitOptions.RemoveEmptyEntries);
        var found = _textAll.Where(r => words.All(w => r.Text.Contains(w, StringComparison.OrdinalIgnoreCase)
            || r.Id.Contains(w, StringComparison.OrdinalIgnoreCase) || r.Hash.Equals(w, StringComparison.OrdinalIgnoreCase))).ToList();
        TextRows = new ObservableCollection<TextRow>(found.Take(5000));
        TextShown = found.Count > 5000 ? $"Showing 5,000 of {found.Count:N0}: search to narrow it down." : words.Length > 0 ? $"{found.Count:N0} found" : "";
    }

    [RelayCommand]
    private Task TextRefresh() => LoadText(true, TextLanguage?.Code);

    [RelayCommand]
    private async Task TextExport()
    {
        if (ExplorerBusy) return;
        ExplorerBusy = true;
        var dest = Path.Combine(ExplorerDest, "Text");
        ExplorerStatus = "Exporting every string in every language…";
        try
        {
            var result = await _discovery.Run("explorer-text-export", Game, "--dest", dest);
            var languages = result["languages"]?.AsArray().Count ?? 0;
            ExplorerStatus = $"Saved {result["count"]?.GetValue<int>():N0} strings in {languages} language{(languages == 1 ? "" : "s")} " +
                $"to strings.csv and strings.json in {dest}.";
        }
        catch (Exception error)
        {
            ExplorerStatus = "Export failed: " + error.Message;
        }
        finally
        {
            ExplorerBusy = false;
        }
    }
}
