using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Media.Imaging;
using Avalonia.Platform.Storage;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FrostyBlender.Services;

namespace FrostyBlender.ViewModels;

/// <summary>A line in the file explorer: a folder or a game file.</summary>
public sealed partial class ExplorerRow : ObservableObject
{
    public required string Name { get; init; }
    public required string Path { get; init; }
    public bool IsFolder { get; init; }
    public bool IsFile => !IsFolder;
    public string Type { get; init; } = "";
    public long Size { get; init; }
    public int Count { get; init; }
    [ObservableProperty] private bool _checked;

    public string Glyph => IsFolder ? "" : Type switch
    {
        "Texture" or "Atlas texture" => "",
        "Mesh data" or "Occluder mesh" or "Cloth mesh" => "",
        "UI movie" or "UI data" or "Font" => "",
        "Animation bank" or "Animation track" => "",
        "EBX" => "",
        _ => "",
    };
    public string Detail => IsFolder ? (Count == 1 ? "1 item" : $"{Count:N0} items") : Type;
    public string SizeText => IsFolder ? "" : Size < 1024 ? $"{Size} B" : Size < 1 << 20 ? $"{Size / 1024.0:0.#} KB" : $"{Size / 1048576.0:0.#} MB";
    public bool CanCheck => IsFile;
}

public sealed record ExplorerField(string Key, string Value);

public partial class MainViewModel
{
    private sealed record ExplorerFile(string Path, string Type, long Size);

    private Dictionary<string, SortedSet<string>> _explorerFolders = new();
    private Dictionary<string, List<ExplorerFile>> _explorerFiles = new();
    private List<ExplorerFile> _explorerAll = new();
    private string _explorerGame = "";
    private int _explorerScan;
    private int _inspectId;

    public ObservableCollection<ExplorerRow> ExplorerRows { get; } = new();
    public ObservableCollection<ExplorerField> ExplorerFields { get; } = new();
    public ObservableCollection<string> ExplorerImports { get; } = new();

    [ObservableProperty] private string _explorerPath = "";
    [ObservableProperty] private string _explorerSearch = "";
    [ObservableProperty] private ExplorerRow? _explorerSelected;
    [ObservableProperty] private bool _explorerLoading;
    [ObservableProperty] private string _explorerError = "";
    [ObservableProperty] private string _explorerStatus = "";
    [ObservableProperty] private string _explorerCount = "";
    [ObservableProperty] private bool _explorerInspecting;
    [ObservableProperty] private string _explorerInfoTitle = "";
    [ObservableProperty] private string _explorerInfoType = "";
    [ObservableProperty] private Bitmap? _explorerPreview;
    [ObservableProperty] private string _explorerDest = "";
    [ObservableProperty] private bool _explorerBusy;
    [ObservableProperty] private int _explorerChecked;

    public bool ExplorerHasInfo => ExplorerInfoTitle.Length > 0;
    public bool ExplorerHasPreview => ExplorerPreview != null;
    public bool ExplorerHasImports => ExplorerImports.Count > 0;
    public bool ExplorerHasError => ExplorerError.Length > 0;
    public bool ExplorerAtRoot => ExplorerPath.Length == 0;
    public string ExplorerPathText => ExplorerSearch.Trim().Length >= 2 ? $"Search results for \"{ExplorerSearch.Trim()}\""
        : ExplorerPath.Length == 0 ? $"{Game} / all files" : $"{Game} / {ExplorerPath}";
    public string ExplorerExportText => ExplorerChecked > 0 ? $"Export {ExplorerChecked} file{(ExplorerChecked == 1 ? "" : "s")}"
        : ExplorerSelected is { IsFile: true } ? "Export this file" : "Export";
    public int ExplorerMeshes => CheckedOrSelected().Count(r => r.Type == "Mesh data");
    public string ExplorerSendText => ExplorerMeshes == 1 ? "Send mesh to Blender" : $"Send {ExplorerMeshes} meshes to Blender";

    partial void OnExplorerInfoTitleChanged(string value) => OnPropertyChanged(nameof(ExplorerHasInfo));
    partial void OnExplorerPreviewChanged(Bitmap? value) => OnPropertyChanged(nameof(ExplorerHasPreview));
    partial void OnExplorerErrorChanged(string value) => OnPropertyChanged(nameof(ExplorerHasError));
    partial void OnExplorerPathChanged(string value)
    {
        OnPropertyChanged(nameof(ExplorerAtRoot));
        OnPropertyChanged(nameof(ExplorerPathText));
        ShowExplorer();
    }
    partial void OnExplorerSearchChanged(string value)
    {
        OnPropertyChanged(nameof(ExplorerPathText));
        ShowExplorer();
    }
    partial void OnExplorerCheckedChanged(int value) => ExplorerSelectionChanged();
    partial void OnExplorerSelectedChanged(ExplorerRow? value)
    {
        ExplorerSelectionChanged();
        if (value == null) return;
        if (value.IsFolder)
        {
            ExplorerSearch = "";
            ExplorerPath = value.Path;
            return;
        }
        _ = Inspect(value.Path, value.Type);
    }

    private void ExplorerSelectionChanged()
    {
        OnPropertyChanged(nameof(ExplorerExportText));
        OnPropertyChanged(nameof(ExplorerMeshes));
        OnPropertyChanged(nameof(ExplorerSendText));
    }

    private List<ExplorerRow> CheckedOrSelected()
    {
        var chosen = _explorerTicks.Values.ToList();
        if (chosen.Count == 0 && ExplorerSelected is { IsFile: true } one) chosen.Add(one);
        return chosen;
    }

    // Ticks survive navigation: a selection can span folders and searches.
    private readonly Dictionary<string, ExplorerRow> _explorerTicks = new();

    private async Task LoadExplorer(bool refresh)
    {
        if (ExplorerDest.Length == 0 || _explorerGame != Game)
            ExplorerDest = Path.Combine(_link.Info?.Work is { Length: > 0 } dir ? dir : Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "Explorer", Game);
        _ = LoadExplorerMedia(false);  // the Sounds or Text list, when one is open
        if (!refresh && _explorerGame == Game && _explorerAll.Count > 0) return;
        var id = ++_explorerScan;
        ExplorerLoading = true;
        ExplorerError = "";
        ExplorerRows.Clear();
        _explorerTicks.Clear();
        ExplorerChecked = 0;
        try
        {
            if (!LinkLoaded)
                throw new InvalidOperationException("Install the Frostbite Map Importer add-on in Blender and open Blender once.");
            var result = await _discovery.Scan(Section.Explorer, Game, refresh);
            if (id != _explorerScan) return;
            var files = new List<ExplorerFile>();
            foreach (var node in result["files"]?.AsArray() ?? new JsonArray())
            {
                var row = node!.AsArray();
                files.Add(new ExplorerFile(row[0]!.GetValue<string>(), row[1]!.GetValue<string>(), row[2]!.GetValue<long>()));
            }
            BuildTree(files);
            _explorerGame = Game;
            ExplorerDest = Path.Combine(_link.Info?.Work is { Length: > 0 } work ? work : Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "Explorer", Game);
            ExplorerCount = $"{files.Count:N0} files";
            ExplorerSearch = "";
            if (ExplorerPath.Length == 0) ShowExplorer();
            else ExplorerPath = "";
        }
        catch (Exception error)
        {
            if (id == _explorerScan) ExplorerError = error.Message;
        }
        finally
        {
            if (id == _explorerScan) ExplorerLoading = false;
        }
    }

    private void BuildTree(List<ExplorerFile> files)
    {
        var folders = new Dictionary<string, SortedSet<string>>(StringComparer.OrdinalIgnoreCase) { [""] = new(StringComparer.OrdinalIgnoreCase) };
        var byFolder = new Dictionary<string, List<ExplorerFile>>(StringComparer.OrdinalIgnoreCase);
        foreach (var file in files)
        {
            var cut = file.Path.LastIndexOf('/');
            var folder = cut < 0 ? "" : file.Path[..cut];
            if (!byFolder.TryGetValue(folder, out var list)) byFolder[folder] = list = new List<ExplorerFile>();
            list.Add(file);
            // Register the folder chain up to the root.
            var child = folder;
            while (child.Length > 0)
            {
                var up = child.LastIndexOf('/');
                var parent = up < 0 ? "" : child[..up];
                if (!folders.TryGetValue(parent, out var subs)) folders[parent] = subs = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
                if (!subs.Add(child)) break;  // already known: so are its parents
                child = parent;
            }
        }
        _explorerFolders = folders;
        _explorerFiles = byFolder;
        _explorerAll = files;
    }

    private int CountUnder(string folder)
    {
        var subs = _explorerFolders.TryGetValue(folder, out var s) ? s.Count : 0;
        var files = _explorerFiles.TryGetValue(folder, out var f) ? f.Count : 0;
        return subs + files;
    }

    private void ShowExplorer()
    {
        ExplorerRows.Clear();
        var query = ExplorerSearch.Trim().ToLowerInvariant();
        IEnumerable<ExplorerRow> rows;
        if (query.Length >= 2)
        {
            var words = query.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            rows = _explorerAll.Where(f => words.All(w => f.Path.Contains(w, StringComparison.OrdinalIgnoreCase)))
                .Take(2000).Select(FileRow);
        }
        else
        {
            var subs = _explorerFolders.TryGetValue(ExplorerPath, out var s) ? s : new SortedSet<string>();
            var files = _explorerFiles.TryGetValue(ExplorerPath, out var f) ? f : new List<ExplorerFile>();
            rows = subs.Select(p => new ExplorerRow { Name = p[(p.LastIndexOf('/') + 1)..], Path = p, IsFolder = true, Count = CountUnder(p) })
                .Concat(files.OrderBy(x => x.Path, StringComparer.OrdinalIgnoreCase).Select(FileRow));
        }
        foreach (var row in rows) ExplorerRows.Add(row);
    }

    private ExplorerRow FileRow(ExplorerFile file)
    {
        var showPath = ExplorerSearch.Trim().Length >= 2;
        var row = new ExplorerRow
        {
            Name = showPath ? file.Path : file.Path[(file.Path.LastIndexOf('/') + 1)..],
            Path = file.Path, Type = file.Type, Size = file.Size,
            Checked = _explorerTicks.ContainsKey(file.Path),
        };
        row.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName != nameof(ExplorerRow.Checked)) return;
            if (row.Checked) _explorerTicks[row.Path] = row; else _explorerTicks.Remove(row.Path);
            ExplorerChecked = _explorerTicks.Count;
        };
        return row;
    }

    [RelayCommand]
    private void ExplorerUp()
    {
        ExplorerSearch = "";
        var cut = ExplorerPath.LastIndexOf('/');
        ExplorerPath = cut < 0 ? "" : ExplorerPath[..cut];
    }

    [RelayCommand]
    private void ExplorerHome()
    {
        ExplorerSearch = "";
        ExplorerPath = "";
    }

    [RelayCommand]
    private Task ExplorerRefresh() => ExplorerIsFiles ? LoadExplorer(true) : LoadExplorerMedia(true);

    [RelayCommand]
    private void ExplorerTickAll()
    {
        var files = ExplorerRows.Where(r => r.IsFile).ToList();
        var tick = files.Any(r => !r.Checked);
        foreach (var row in files) row.Checked = tick;
    }

    [RelayCommand]
    private void ExplorerClear()
    {
        foreach (var row in _explorerTicks.Values.ToList()) row.Checked = false;
        _explorerTicks.Clear();
        ExplorerChecked = 0;
    }

    /// <summary>Jump to an imported file: open its folder and show it.</summary>
    [RelayCommand]
    private void ExplorerGoTo(string path)
    {
        var file = _explorerAll.FirstOrDefault(f => f.Path.Equals(path, StringComparison.OrdinalIgnoreCase));
        if (file == null)
        {
            ExplorerStatus = path + " isn't in this game's file list.";
            return;
        }
        ExplorerSearch = "";
        var cut = file.Path.LastIndexOf('/');
        ExplorerPath = cut < 0 ? "" : file.Path[..cut];
        ExplorerSelected = ExplorerRows.FirstOrDefault(r => r.Path == file.Path);
    }

    private async Task Inspect(string path, string type)
    {
        var id = ++_inspectId;
        ExplorerInspecting = true;
        ExplorerInfoTitle = path[(path.LastIndexOf('/') + 1)..];
        ExplorerInfoType = type;
        ExplorerPreview = null;
        ExplorerFields.Clear();
        ExplorerImports.Clear();
        OnPropertyChanged(nameof(ExplorerHasImports));
        try
        {
            var info = await _discovery.Run("explorer-inspect", Game, "--name", path);
            if (id != _inspectId) return;
            ExplorerFields.Add(new ExplorerField("Path", path));
            if (info["root_type"] is { } root) ExplorerFields.Add(new ExplorerField("Type", root.GetValue<string>()));
            if (info["resource_type"] is { } res) ExplorerFields.Add(new ExplorerField("Resource", res.GetValue<string>()));
            if (info["ebx_bytes"] is { } eb) ExplorerFields.Add(new ExplorerField("EBX size", $"{eb.GetValue<long>():N0} bytes"));
            if (info["resource_bytes"] is { } rb) ExplorerFields.Add(new ExplorerField("Resource size", $"{rb.GetValue<long>():N0} bytes"));
            if (info["objects"] is { } objects) ExplorerFields.Add(new ExplorerField("Objects", objects.ToString()));
            if (info["images"] is { } images)
                ExplorerFields.Add(new ExplorerField("Images", images.GetValue<int>() == 1 ? "Exports 1 image" : $"Exports {images.GetValue<int>():N0} images"));
            if (info["movie"]?["shapes"] is { } shapes)
                ExplorerFields.Add(new ExplorerField("Vector shapes", $"{shapes.GetValue<int>():N0} (not exported)"));
            foreach (var (key, value) in info["fields"]?.AsObject() ?? new JsonObject())
                ExplorerFields.Add(new ExplorerField(key, value is JsonValue v ? v.ToString() : value?.ToJsonString() ?? ""));
            foreach (var name in info["imports"]?.AsArray() ?? new JsonArray())
                ExplorerImports.Add(name!.GetValue<string>());
            OnPropertyChanged(nameof(ExplorerHasImports));
            if (info["preview"]?.GetValue<string>() is { Length: > 0 } png && File.Exists(png))
                ExplorerPreview = new Bitmap(png);
        }
        catch (Exception error)
        {
            if (id == _inspectId) ExplorerFields.Add(new ExplorerField("Error", error.Message));
        }
        finally
        {
            if (id == _inspectId) ExplorerInspecting = false;
        }
    }

    [RelayCommand]
    private async Task ExplorerChooseFolder()
    {
        var window = (Application.Current?.ApplicationLifetime as IClassicDesktopStyleApplicationLifetime)?.MainWindow;
        if (window == null) return;
        var folders = await window.StorageProvider.OpenFolderPickerAsync(new FolderPickerOpenOptions
        {
            Title = "Export game files to…",
            AllowMultiple = false,
        });
        if (folders.Count > 0 && folders[0].TryGetLocalPath() is { } local) ExplorerDest = local;
    }

    [RelayCommand]
    private async Task ExplorerExport()
    {
        var chosen = CheckedOrSelected();
        if (chosen.Count == 0 || ExplorerBusy)
        {
            ExplorerStatus = "Tick files (or click one) to export.";
            return;
        }
        ExplorerBusy = true;
        ExplorerStatus = $"Exporting {chosen.Count} file{(chosen.Count == 1 ? "" : "s")}…";
        var items = Path.Combine(Path.GetTempPath(), $"frostyblender_items_{Guid.NewGuid():N}.json");
        try
        {
            Directory.CreateDirectory(ExplorerDest);
            await File.WriteAllTextAsync(items, new JsonArray(chosen.Select(r => (JsonNode)r.Path).ToArray()).ToJsonString());
            var result = await _discovery.Run("explorer-export", Game, "--items", items, "--dest", ExplorerDest,
                "--texture-size", TextureSize);
            var written = result["written"]?.AsArray().Count ?? 0;
            var errors = result["errors"]?.AsArray() ?? new JsonArray();
            ExplorerStatus = $"Saved {written} file{(written == 1 ? "" : "s")} to {ExplorerDest}" +
                (errors.Count > 0 ? $". {errors.Count} couldn't be exported: {errors[0]?["name"]} ({errors[0]?["error"]})" : ".");
        }
        catch (Exception error)
        {
            ExplorerStatus = "Export failed: " + error.Message;
        }
        finally
        {
            try { File.Delete(items); } catch (IOException) { }
            ExplorerBusy = false;
        }
    }

    [RelayCommand]
    private async Task ExplorerSendToBlender()
    {
        var meshes = CheckedOrSelected().Where(r => r.Type == "Mesh data").ToList();
        if (meshes.Count == 0)
        {
            ExplorerStatus = "Tick one or more meshes (files of type Mesh data) to send them to Blender.";
            return;
        }
        if (!Connected)
        {
            ExplorerStatus = "Open Blender with the add-on to send meshes.";
            return;
        }
        ExplorerBusy = true;
        ExplorerStatus = "Finding the meshes' textures…";
        var items = Path.Combine(Path.GetTempPath(), $"frostyblender_items_{Guid.NewGuid():N}.json");
        try
        {
            await File.WriteAllTextAsync(items, new JsonArray(meshes.Select(r => (JsonNode)r.Path).ToArray()).ToJsonString());
            var tables = await _discovery.Run("explorer-meshes", Game, "--items", items);
            var body = new JsonObject
            {
                ["kind"] = "props", ["game"] = Game,
                ["options"] = new JsonObject { ["texture_size"] = TextureSize, ["game_shaders"] = GameShaders },
                ["map"] = new JsonObject
                {
                    ["name"] = "Explorer", ["package"] = "explorer",
                    ["variation_dbs"] = tables["variation_dbs"]?.DeepClone() ?? new JsonArray(),
                },
                ["items"] = new JsonArray(meshes.Select(r => (JsonNode)new JsonObject
                {
                    ["name"] = r.Path[(r.Path.LastIndexOf('/') + 1)..], ["group"] = "explorer",
                    ["asset"] = r.Path, ["bundle"] = "asset:" + r.Path,
                }).ToArray()),
            };
            _job = await _link.Import(body);
            _jobKind = "props";
            ExplorerStatus = $"Sent {meshes.Count} mesh{(meshes.Count == 1 ? "" : "es")} to Blender. They arrive in a row in a new collection.";
        }
        catch (Exception error)
        {
            ExplorerStatus = "Couldn't send: " + error.Message;
        }
        finally
        {
            try { File.Delete(items); } catch (IOException) { }
            ExplorerBusy = false;
        }
    }
}
