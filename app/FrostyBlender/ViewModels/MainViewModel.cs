using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Avalonia.Media.Imaging;
using Avalonia.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using FrostyBlender.Services;

namespace FrostyBlender.ViewModels;

public sealed record NavItem(Section Section, string Title, string Glyph);

public partial class MainViewModel : ObservableObject
{
    public const string DiscordUrl = "https://discord.gg/Vh7cgGzUyB";
    public const string GitHubUrl = "https://github.com/4FingerStudios/FrostyBlender";

    private readonly BlenderLink _link = new();
    private readonly Discovery _discovery;
    private readonly Icons _icons;
    private readonly DispatcherTimer _timer;
    private List<Entry> _all = new();
    private string _startSelect = "";

    private static string StartOption(string name)
    {
        var args = Program.Args;
        var at = Array.IndexOf(args, name);
        return at >= 0 && at + 1 < args.Length ? args[at + 1] : "";
    }
    private string? _job;
    private string _jobKind = "";

    public MainViewModel()
    {
        _discovery = new Discovery(_link);
        _icons = new Icons(_link);
        _selectedNav = Nav[0];
        _startSelect = StartOption("--select");
        if (Enum.TryParse<Section>(StartOption("--section"), true, out var startSection))
            _selectedNav = Nav.First(n => n.Section == startSection);
        if (Games.Contains(StartOption("--game").ToUpperInvariant())) _game = StartOption("--game").ToUpperInvariant();
        _linkLoaded = _link.Load();
        _timer = new DispatcherTimer(TimeSpan.FromSeconds(1.5), DispatcherPriority.Background, async (_, _) => await Tick());
        _timer.Start();
        _ = Tick();
        if (LinkLoaded) _ = Scan(false);
    }

    public IReadOnlyList<NavItem> Nav { get; } = new[]
    {
        new NavItem(Section.Maps, "Maps", "\uE909"),
        new NavItem(Section.Characters, "Characters", "\uE77B"),
        new NavItem(Section.Npcs, "NPCs", "\uE99A"),
        new NavItem(Section.Props, "Props", "\uE7B8"),
        new NavItem(Section.Effects, "Effects", "\uE945"),
        new NavItem(Section.Animations, "Animations", "\uE714"),
        new NavItem(Section.Settings, "Settings", "\uE713"),
    };

    public IReadOnlyList<string> Games { get; } = new[] { "GW1", "GW2", "BFN" };
    public IReadOnlyList<string> TextureSizes { get; } = new[] { "512", "1024", "2048" };

    public ObservableCollection<Entry> Entries { get; } = new();
    public ObservableCollection<Row> Rows { get; } = new();
    public ObservableCollection<string> Armatures { get; } = new() { ActiveArmature };
    public const string ActiveArmature = "Active armature in Blender";

    [ObservableProperty] private NavItem _selectedNav = null!;
    [ObservableProperty] private string _game = "GW2";
    [ObservableProperty] private string _search = "";
    [ObservableProperty] private string _itemSearch = "";
    [ObservableProperty] private Entry? _selectedEntry;
    [ObservableProperty] private bool _scanning;
    [ObservableProperty] private string _scanMessage = "";
    [ObservableProperty] private string _error = "";
    [ObservableProperty] private bool _linkLoaded;
    [ObservableProperty] private bool _connected;
    [ObservableProperty] private string _blenderStatus = "Looking for Blender…";
    [ObservableProperty] private string _jobStatus = "";
    [ObservableProperty] private bool _sending;
    [ObservableProperty] private string _textureSize = "1024";
    [ObservableProperty] private bool _rig = true;
    [ObservableProperty] private bool _gameShaders;
    [ObservableProperty] private bool _spawnAtCursor = true;
    [ObservableProperty] private bool _setFps = true;
    [ObservableProperty] private bool _hideAdditive = true;
    [ObservableProperty] private string _armature = ActiveArmature;
    [ObservableProperty] private int _ticked;

    public Section Section => SelectedNav?.Section ?? Section.Maps;
    public bool IsBrowser => Section != Section.Settings;
    public bool IsSettings => Section == Section.Settings;
    public bool HasItems => Section is not Section.Maps;
    public bool IsCharacterLike => Section is Section.Characters or Section.Npcs;
    public bool IsNpcs => Section == Section.Npcs;
    public bool ShowCursorOption => Section is Section.Npcs or Section.Effects;
    public bool IsAnimations => Section == Section.Animations;
    public bool HasSelection => SelectedEntry != null;
    public bool ShowPreview => SelectedEntry is { Wide: true, HasIcon: true };
    public bool HasError => Error.Length > 0;
    public bool ShowEmpty => !Scanning && !HasError && Entries.Count == 0 && LinkLoaded;
    public string SectionTitle => SelectedNav?.Title ?? "";
    public string SectionBlurb => Section switch
    {
        Section.Maps => "Whole levels, terrain and all, straight into Blender.",
        Section.Characters => "Heroes and every hat, head and accessory they own.",
        Section.Npcs => "Horde, bosses, turrets, vehicles and abilities like the Chili Bean bomb.",
        Section.Props => "Single props and buildings, picked from any map.",
        Section.Effects => "Explosions, smoke and sparks that play in Blender.",
        Section.Animations => "Game animation clips for the characters you imported.",
        _ => "",
    };
    public string CountText => Entries.Count == 0 ? "" : $"{Entries.Count} " + Section switch
    {
        Section.Maps or Section.Props => Entries.Count == 1 ? "map" : "maps",
        Section.Npcs => Entries.Count == 1 ? "NPC" : "NPCs",
        Section.Effects => Entries.Count == 1 ? "group" : "groups",
        _ => Entries.Count == 1 ? "character" : "characters",
    };
    public string SendText => Section switch
    {
        Section.Maps => "Import map in Blender",
        Section.Animations => Ticked > 0 ? $"Send {Ticked} clip{(Ticked == 1 ? "" : "s")} to Blender" : "Send clip to Blender",
        _ => Ticked > 0 ? $"Send {Ticked} item{(Ticked == 1 ? "" : "s")} to Blender" : "Send to Blender",
    };
    public string ItemsHeader => Section switch
    {
        Section.Props => "PROPS",
        Section.Effects => "EFFECTS",
        Section.Animations => "CLIPS",
        _ => "ITEMS",
    };
    public string Hint => Section switch
    {
        Section.Maps => "Big maps take a few minutes. Blender stays usable while they convert.",
        Section.Characters => "Tick a body, head and props. Pieces added later join the same rig.",
        Section.Npcs => "Tick a body, then costumes, props, weapons or abilities. Ability objects come rigged, ready for their animations.",
        Section.Props => "Props come in a row along X, each in its own collection.",
        Section.Effects => "Effects play from frame 1 with Geometry Nodes; press play in Blender. Quads face the scene camera.",
        Section.Animations => "Import the character first, then send its clips.",
        _ => "",
    };

    // link.json details for Settings
    public string LinkFile => BlenderLink.LinkFile;
    public LinkInfo? Info => _link.Info;
    public string AppVersion => typeof(MainViewModel).Assembly.GetName().Version?.ToString(3) ?? "";

    partial void OnSelectedNavChanged(NavItem value)
    {
        foreach (var name in new[] { nameof(Section), nameof(IsBrowser), nameof(IsSettings), nameof(HasItems),
                     nameof(IsCharacterLike), nameof(IsNpcs), nameof(ShowCursorOption), nameof(IsAnimations), nameof(SectionTitle), nameof(SectionBlurb),
                     nameof(SendText), nameof(ItemsHeader), nameof(Hint) })
            OnPropertyChanged(name);
        if (IsBrowser) _ = Scan(false);
    }

    partial void OnGameChanged(string value) => _ = Scan(false);
    partial void OnSearchChanged(string value) => Filter();
    partial void OnItemSearchChanged(string value) => BuildRows();
    partial void OnHideAdditiveChanged(bool value) => BuildRows();
    partial void OnErrorChanged(string value) { OnPropertyChanged(nameof(HasError)); OnPropertyChanged(nameof(ShowEmpty)); }
    partial void OnScanningChanged(bool value) => OnPropertyChanged(nameof(ShowEmpty));
    partial void OnTickedChanged(int value) => OnPropertyChanged(nameof(SendText));

    partial void OnSelectedEntryChanged(Entry? value)
    {
        OnPropertyChanged(nameof(HasSelection));
        OnPropertyChanged(nameof(ShowPreview));
        if (value != null)
            value.PropertyChanged += (_, e) =>
            {
                if (e.PropertyName == nameof(Entry.HasIcon) && ReferenceEquals(value, SelectedEntry)) OnPropertyChanged(nameof(ShowPreview));
            };
        if (value != null && IsCharacterLike && !value.ItemIconsRequested)
        {
            value.ItemIconsRequested = true;
            _ = LoadItemIcons(Game, value);
        }
        ItemSearch = "";
        BuildRows();
    }

    [RelayCommand]
    private async Task Refresh() => await Scan(true);

    /// <summary>Jump from an animated rig to the model it animates, selected in its own section.</summary>
    [RelayCommand]
    private void ShowModel()
    {
        if (SelectedEntry is not { HasModel: true } entry) return;
        var section = entry.ModelSection == "Characters" ? Section.Characters : Section.Npcs;
        _startSelect = entry.ModelId;
        SelectedNav = Nav.First(n => n.Section == section);
    }

    [RelayCommand]
    private async Task Retry()
    {
        LinkLoaded = _link.Load();
        OnPropertyChanged(nameof(Info));
        await Tick();
        if (LinkLoaded) await Scan(false);
    }

    private int _scanId;

    private async Task Scan(bool refresh)
    {
        if (!IsBrowser) return;
        var id = ++_scanId;
        var section = Section;
        Error = "";
        Scanning = true;
        ScanMessage = refresh ? $"Rescanning {Game}…" : $"Reading {Game} {SectionTitle.ToLowerInvariant()}…";
        Entries.Clear();
        SelectedEntry = null;
        _all.Clear();
        OnPropertyChanged(nameof(CountText));
        try
        {
            if (!LinkLoaded)
                throw new InvalidOperationException(
                    "Install the Frostbite Map Importer add-on in Blender and open Blender once, then click Retry.");
            var result = await _discovery.Scan(section, Game, refresh);
            if (id != _scanId) return;
            _all = Build(section, result);
            Filter();
            _ = LoadPortraits(section, Game, _all);
            if (_startSelect.Length > 0)
            {
                SelectedEntry = Entries.FirstOrDefault(e => e.IconId == _startSelect || e.Title.Equals(_startSelect, StringComparison.OrdinalIgnoreCase));
                _startSelect = "";
            }
        }
        catch (Exception error)
        {
            if (id == _scanId) Error = error.Message;
        }
        finally
        {
            if (id == _scanId) Scanning = false;
        }
    }

    private void Filter()
    {
        var keep = SelectedEntry;
        var words = Search.Trim().ToLowerInvariant().Split(' ', StringSplitOptions.RemoveEmptyEntries);
        Entries.Clear();
        foreach (var entry in _all.Where(e => words.All(w => e.Search.Contains(w))))
            Entries.Add(entry);
        if (keep != null && Entries.Contains(keep)) SelectedEntry = keep;
        OnPropertyChanged(nameof(CountText));
        OnPropertyChanged(nameof(ShowEmpty));
    }

    private void BuildRows()
    {
        Rows.Clear();
        if (SelectedEntry == null) { Ticked = 0; return; }
        var words = ItemSearch.Trim().ToLowerInvariant().Split(' ', StringSplitOptions.RemoveEmptyEntries);
        var shown = SelectedEntry.Items
            .Where(i => !(IsAnimations && HideAdditive && i.Group == "Additive"))
            .Where(i => words.All(w => i.Name.ToLowerInvariant().Contains(w) || i.Group.ToLowerInvariant().Contains(w)));
        foreach (var group in shown.GroupBy(i => i.Group))
        {
            Rows.Add(new Row { Name = group.Key, IsHeader = true, Detail = group.Count().ToString(CultureInfo.InvariantCulture) });
            foreach (var item in group)
            {
                var row = new Row { Name = item.Name, Detail = item.Detail, Item = item, Selected = item.Selected };
                row.Watch();
                row.PropertyChanged += (_, e) => { if (e.PropertyName == nameof(Row.Selected)) CountTicked(); };
                Rows.Add(row);
            }
        }
        CountTicked();
    }

    private void CountTicked() => Ticked = SelectedEntry?.Items.Count(i => i.Selected) ?? 0;

    [RelayCommand]
    private void SelectAll()
    {
        foreach (var row in Rows.Where(r => r.IsItem)) row.Selected = true;
    }

    [RelayCommand]
    private void SelectNone()
    {
        foreach (var item in SelectedEntry?.Items ?? new()) item.Selected = false;
        foreach (var row in Rows.Where(r => r.IsItem)) row.Selected = false;
    }

    // ------------------------------------------------------------ building tiles
    private static string Str(JsonNode? node, string key) => node?[key]?.GetValue<string>() ?? "";

    private static string Count(int n, string noun) => $"{n} {noun}{(n == 1 ? "" : "s")}";

    private static string Title(string text) => CultureInfo.InvariantCulture.TextInfo.ToTitleCase(text.Replace('_', ' ').Trim());

    private static List<Entry> Build(Section section, JsonObject result)
    {
        var list = new List<Entry>();
        switch (section)
        {
            case Section.Maps:
                foreach (var map in result["maps"]?.AsArray() ?? new JsonArray())
                {
                    var node = map!.AsObject();
                    var package = Str(node, "package");
                    list.Add(new Entry
                    {
                        Title = Str(node, "name"), Subtitle = package.Split('/').LastOrDefault() ?? "",
                        Accent = Palette.Maps, Node = node, Search = (Str(node, "name") + " " + package).ToLowerInvariant(),
                        Wide = true, IconId = package,
                    });
                }
                break;
            case Section.Characters:
            case Section.Npcs:
                foreach (var character in result["characters"]?.AsArray() ?? new JsonArray())
                {
                    var node = character!.AsObject();
                    var items = new List<Item>();
                    foreach (var (slot, values) in node["slots"]?.AsObject() ?? new JsonObject())
                        foreach (var value in values!.AsArray())
                            items.Add(new Item { Name = Str(value, "name"), Group = Title(slot), Slot = slot, Node = value!.AsObject() });
                    items = items.OrderBy(i => i.Group).ThenBy(i => i.Name, StringComparer.OrdinalIgnoreCase).ToList();
                    var team = Str(node, "team");
                    var group = Str(node, "group");
                    list.Add(new Entry
                    {
                        Title = Str(node, "name"), Subtitle = Count(items.Count, "item"), Badge = group.Length > 0 ? group : team,
                        IconId = Str(node, "id"),
                        Accent = Palette.Team(team), Node = node, Items = items,
                        Search = $"{Str(node, "name")} {Str(node, "id")} {team} {group}".ToLowerInvariant(),
                    });
                }
                break;
            case Section.Props:
                foreach (var map in result["maps"]?.AsArray() ?? new JsonArray())
                {
                    var node = map!.AsObject();
                    var items = (node["props"]?.AsArray() ?? new JsonArray())
                        .Select(p => new Item { Name = Str(p, "name"), Group = Str(p, "group"), Node = p!.AsObject() })
                        .OrderBy(i => i.Group).ThenBy(i => i.Name).ToList();
                    list.Add(new Entry
                    {
                        Title = Str(node, "name"), Subtitle = Count(items.Count, "prop"), Accent = Palette.Props, Node = node,
                        Wide = true, IconId = Str(node, "package"),
                        Items = items, Search = (Str(node, "name") + " " + Str(node, "package")).ToLowerInvariant(),
                    });
                }
                break;
            case Section.Effects:
                foreach (var group in result["groups"]?.AsArray() ?? new JsonArray())
                {
                    var node = group!.AsObject();
                    var name = Str(node, "name");
                    var items = (node["effects"]?.AsArray() ?? new JsonArray())
                        .Select(e => new Item { Name = Str(e, "name"), Group = EffectGroup(Str(e, "asset")), Node = e!.AsObject() })
                        .OrderBy(i => i.Group).ThenBy(i => i.Name).ToList();
                    var parts = name.Split('/');
                    list.Add(new Entry
                    {
                        Title = Title(parts[^1]), Subtitle = Count(items.Count, "effect"), Badge = parts.Length > 1 ? Title(parts[0]) : "",
                        Accent = Palette.Effects, Node = node, Items = items,
                        Search = (name + " " + string.Join(' ', items.Select(i => i.Name))).ToLowerInvariant(),
                    });
                }
                break;
            case Section.Animations:
                foreach (var character in result["characters"]?.AsArray() ?? new JsonArray())
                {
                    var node = character!.AsObject();
                    var name = Str(node, "name");
                    var items = (node["clips"]?.AsArray() ?? new JsonArray()).Select(c => new Item
                    {
                        Name = Str(c, "name"), Group = ClipGroup(c!, name), Node = c!.AsObject(),
                        Detail = $"{c!["frames"]?.GetValue<int>() ?? 0} f",
                    }).OrderBy(i => i.Group == "Additive").ThenBy(i => i.Group).ThenBy(i => i.Name).ToList();
                    var team = name.StartsWith("PT ") ? "Plants" : name.StartsWith("Z") && name.Length > 2 && name[2] == ' ' ? "Zombies" : "";
                    list.Add(new Entry
                    {
                        Title = name, Subtitle = Count(items.Count, "clip"), Badge = Str(node, "rig"), Accent = Palette.Team(team),
                        IconId = RigIconId(name),
                        ModelName = Str(node["model"], "name"), ModelId = Str(node["model"], "id"),
                        ModelSection = Str(node["model"], "section"),
                        Node = node, Items = items, Search = (name + " " + Str(node, "rig")).ToLowerInvariant(),
                    });
                }
                break;
        }
        return list;
    }

    /// <summary>fx/plants/peashooter/chilibeanbomb/fx_... -> Chilibeanbomb (the folder the effect sits in).</summary>
    private static string EffectGroup(string asset)
    {
        var parts = asset.Split('/');
        return parts.Length > 1 ? Title(parts[^2]) : "Effects";
    }

    /// <summary>Peashooter_Gesture_Dance -> Gesture; additive layers get their own group.</summary>
    private static string ClipGroup(JsonNode clip, string character)
    {
        if (clip["additive"]?.GetValue<bool>() == true) return "Additive";
        var who = character.Replace(" ", "").ToLowerInvariant();
        var token = Str(clip, "name").Split('_', StringSplitOptions.RemoveEmptyEntries)
            .FirstOrDefault(t => !who.Contains(t.ToLowerInvariant()) && !t.ToLowerInvariant().Contains(who) && !t.All(char.IsDigit));
        return token == null ? "Other" : Title(token);
    }

    // ------------------------------------------------------------ sending
    [RelayCommand]
    private async Task Send()
    {
        if (SelectedEntry == null || Sending) return;
        var entry = SelectedEntry;
        var chosen = entry.Items.Where(i => i.Selected).ToList();
        if (HasItems && chosen.Count == 0)
        {
            JobStatus = "Tick at least one item first.";
            return;
        }
        var options = new JsonObject { ["texture_size"] = TextureSize, ["rig"] = Rig, ["spawn_at_cursor"] = SpawnAtCursor, ["set_fps"] = SetFps, ["game_shaders"] = GameShaders };
        if (Section == Section.Animations && Armature != ActiveArmature) options["armature"] = Armature;
        var kind = Section switch
        {
            Section.Maps => "maps", Section.Characters => "cosmetics", Section.Npcs => "npcs",
            Section.Props => "props", Section.Effects => "effects", _ => "animations",
        };
        var body = new JsonObject { ["kind"] = kind, ["game"] = Game, ["options"] = options };
        var node = entry.Node;
        switch (Section)
        {
            case Section.Maps:
                body["map"] = new JsonObject { ["name"] = Str(node, "name"), ["package"] = Str(node, "package"), ["mod"] = Str(node, "mod") };
                break;
            case Section.Characters:
            case Section.Npcs:
                body["character"] = new JsonObject
                {
                    ["id"] = Str(node, "id"), ["name"] = Str(node, "name"), ["team"] = Str(node, "team"), ["group"] = Str(node, "group"),
                };
                body["items"] = new JsonArray(chosen.Select(i =>
                {
                    var copy = i.Node.DeepClone().AsObject();
                    copy["slot"] = i.Slot.Length > 0 ? i.Slot : i.Group.ToLowerInvariant();
                    return (JsonNode)copy;
                }).ToArray());
                break;
            case Section.Props:
                body["map"] = new JsonObject
                {
                    ["name"] = Str(node, "name"), ["package"] = Str(node, "package"),
                    ["variation_dbs"] = node["variation_dbs"]?.DeepClone() ?? new JsonArray(),
                };
                body["items"] = new JsonArray(chosen.Select(i => (JsonNode)i.Node.DeepClone()).ToArray());
                break;
            case Section.Effects:
                body["group"] = new JsonObject { ["name"] = Str(node, "name") };
                body["items"] = new JsonArray(chosen.Select(i => (JsonNode)i.Node.DeepClone()).ToArray());
                break;
            case Section.Animations:
                body["character"] = new JsonObject { ["rig"] = Str(node, "rig"), ["name"] = Str(node, "name") };
                body["items"] = new JsonArray(chosen.Select(i => (JsonNode)i.Node.DeepClone()).ToArray());
                break;
        }
        Sending = true;
        try
        {
            _job = await _link.Import(body);
            _jobKind = kind;
            JobStatus = $"Sent {entry.Title} to Blender…";
        }
        catch (Exception error)
        {
            JobStatus = error.Message;
        }
        finally
        {
            Sending = false;
        }
    }

    // ------------------------------------------------------------ game icons
    /// <summary>"PT PeaCannon" -> "peacannon", "ZT LaserTurret" -> "laserturret", "PeaShooter" -> "peashooter".</summary>
    private static string RigIconId(string name)
    {
        var words = name.Split(' ', StringSplitOptions.RemoveEmptyEntries);
        if (words.Length > 1 && words[0].Length == 2 && words[0].All(char.IsUpper)) words = words[1..];
        return string.Concat(words).ToLowerInvariant();
    }

    private static Bitmap? LoadBitmap(string? path)
    {
        if (path == null) return null;
        try { return new Bitmap(path); }
        catch (Exception) { return null; }
    }

    private async Task LoadPortraits(Section section, string game, List<Entry> entries)
    {
        if (section is Section.Settings or Section.Effects || entries.Count == 0) return;
        if (_icons.Problem is { } problem)
        {
            JobStatus = problem;
            return;
        }
        var maps = section is Section.Maps or Section.Props;
        var byKey = entries.GroupBy(e => (maps ? "m:" : "p:") + e.IconId).ToDictionary(g => g.Key, g => g.ToList());
        void Apply(Dictionary<string, string?> found)
        {
            foreach (var (key, path) in found)
                if (path != null && byKey.TryGetValue(key, out var list))
                {
                    var bitmap = LoadBitmap(path);
                    foreach (var entry in list.Where(e => e.Icon == null)) entry.Icon = bitmap;
                }
        }
        Apply(_icons.Cached(game, byKey.Keys));
        if (maps)
        {
            var mapRequests = byKey.Select(kv => new JsonObject
            {
                ["key"] = kv.Key, ["package"] = kv.Value[0].IconId, ["name"] = kv.Value[0].Title,
            }).ToList();
            Apply(await _icons.Fetch(game, Array.Empty<JsonObject>(), Array.Empty<JsonObject>(), mapRequests));
            return;
        }
        var requests = byKey.Select(kv => new JsonObject { ["key"] = kv.Key, ["id"] = kv.Value[0].IconId, ["name"] = kv.Value[0].Title }).ToList();
        Apply(await _icons.Fetch(game, requests, Array.Empty<JsonObject>()));
    }

    private async Task LoadItemIcons(string game, Entry entry)
    {
        if (_icons.Problem != null) return;
        var byKey = entry.Items.GroupBy(i => "i:" + Str(i.Node, "bundle")).ToDictionary(g => g.Key, g => g.ToList());
        void Apply(Dictionary<string, string?> found)
        {
            foreach (var (key, path) in found)
                if (path != null && byKey.TryGetValue(key, out var list))
                {
                    var bitmap = LoadBitmap(path);
                    foreach (var item in list.Where(i => i.Icon == null)) item.Icon = bitmap;
                }
        }
        Apply(_icons.Cached(game, byKey.Keys));
        var requests = byKey.Select(kv => new JsonObject
        {
            ["key"] = kv.Key, ["character"] = entry.IconId, ["slot"] = kv.Value[0].Slot,
            ["name"] = kv.Value[0].Name, ["bundle"] = Str(kv.Value[0].Node, "bundle"),
        }).ToList();
        Apply(await _icons.Fetch(game, Array.Empty<JsonObject>(), requests));
    }

    // ------------------------------------------------------------ Blender status
    private async Task Tick()
    {
        // Blender rewrites link.json every few seconds; pick up a newer add-on without a restart.
        var hadIcons = _icons.Problem == null && _link.Info != null;
        LinkLoaded = _link.Load() || LinkLoaded;
        if (!hadIcons && _link.Info != null && _icons.Problem == null && IsBrowser && _all.Count > 0)
        {
            if (JobStatus.StartsWith("Icons and loading screens")) JobStatus = "";
            _ = LoadPortraits(Section, Game, _all);
            if (SelectedEntry is { } selected && IsCharacterLike)
            {
                selected.ItemIconsRequested = true;
                _ = LoadItemIcons(Game, selected);
            }
        }
        var ping = await _link.Get("/ping");
        Connected = ping != null;
        BlenderStatus = Connected
            ? $"Blender {Str(ping, "blender_version")} connected"
            : LinkLoaded ? "Blender isn't open" : "Add-on not set up yet";
        if (!Connected) return;
        if (_job != null)
        {
            var status = await _link.Get("/status");
            var job = status?["jobs"]?[_job];
            if (job != null)
            {
                var state = Str(job, "state");
                var message = Str(job, "message");
                JobStatus = state switch
                {
                    "done" => "✔ " + message,
                    "failed" => "✖ " + message,
                    _ => message.Length > 0 ? message : "Working…",
                };
                if (state is "done" or "failed") _job = null;
            }
        }
        if (IsAnimations)
        {
            var arms = await _link.Get("/armatures");
            var names = (arms?["armatures"]?.AsArray() ?? new JsonArray()).Select(a => Str(a, "name")).ToList();
            var current = Armatures.Skip(1).ToList();
            if (!names.SequenceEqual(current))
            {
                var keep = Armature;
                while (Armatures.Count > 1) Armatures.RemoveAt(1);
                foreach (var name in names) Armatures.Add(name);
                Armature = Armatures.Contains(keep) ? keep : ActiveArmature;
            }
        }
    }

    [RelayCommand]
    private void Open(string target)
    {
        try { Process.Start(new ProcessStartInfo(target) { UseShellExecute = true }); }
        catch (Exception) { /* nothing to open */ }
    }
}
