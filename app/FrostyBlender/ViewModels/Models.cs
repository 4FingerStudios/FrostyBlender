using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using Avalonia.Media;
using Avalonia.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;

namespace FrostyBlender.ViewModels;

/// <summary>A tile in the browser: a map, a character, an NPC or an animated rig.</summary>
public sealed partial class Entry : ObservableObject
{
    [ObservableProperty] [NotifyPropertyChangedFor(nameof(HasIcon))] private Bitmap? _icon;
    public bool HasIcon => Icon != null;
    public bool NoIcon => Icon == null;
    partial void OnIconChanged(Bitmap? value) => OnPropertyChanged(nameof(NoIcon));
    /// <summary>The id the icons tool matches portraits by (character id, or an animated rig's name).</summary>
    public string IconId { get; init; } = "";
    public bool ItemIconsRequested { get; set; }
    /// <summary>Maps show their loading screen as a wide 16:9 card.</summary>
    public bool Wide { get; init; }
    public double TileWidth => Wide ? 190 : 148;
    public double ImageHeight => Wide ? 107 : 148;

    public required string Title { get; init; }
    public string Subtitle { get; init; } = "";
    public string Badge { get; init; } = "";
    public required IBrush Accent { get; init; }
    public required JsonObject Node { get; init; }
    public List<Item> Items { get; init; } = new();

    public string Initials
    {
        get
        {
            var words = Title.Split(' ', '_', '-').Where(w => w.Length > 0).ToArray();
            return words.Length switch
            {
                0 => "?",
                1 => words[0][..System.Math.Min(2, words[0].Length)].ToUpperInvariant(),
                _ => $"{char.ToUpperInvariant(words[0][0])}{char.ToUpperInvariant(words[1][0])}",
            };
        }
    }

    public string Search { get; init; } = "";

    /// <summary>For an animated rig: the model its clips play on, and the section that imports it.</summary>
    public string ModelName { get; init; } = "";
    public string ModelId { get; init; } = "";
    public string ModelSection { get; init; } = "";
    public bool HasModel => ModelId.Length > 0;
    public string ModelHint => ModelSection == "Characters"
        ? "Import it rigged from Characters, then send these clips."
        : "Import it rigged from NPCs, then send these clips.";
}

/// <summary>A row in the detail list: a group header or a tickable item.</summary>
public partial class Row : ObservableObject
{
    public string Name { get; init; } = "";
    public string Detail { get; init; } = "";
    public bool IsHeader { get; init; }
    public bool IsItem => !IsHeader;
    public Item? Item { get; init; }
    public bool HasIcon => Item?.HasIcon == true;

    /// <summary>Rows follow their item's icon as it arrives.</summary>
    public void Watch()
    {
        if (Item != null)
            Item.PropertyChanged += (_, e) => { if (e.PropertyName == nameof(Item.HasIcon)) OnPropertyChanged(nameof(HasIcon)); };
    }

    [ObservableProperty] private bool _selected;

    partial void OnSelectedChanged(bool value)
    {
        if (Item != null) Item.Selected = value;
    }
}

public sealed partial class Item : ObservableObject
{
    [ObservableProperty] [NotifyPropertyChangedFor(nameof(HasIcon))] private Bitmap? _icon;
    public bool HasIcon => Icon != null;
    public required string Name { get; init; }
    public required string Group { get; init; }
    /// <summary>The raw slot key from the discover result (cosmetics and NPCs).</summary>
    public string Slot { get; init; } = "";
    public string Detail { get; init; } = "";
    public required JsonObject Node { get; init; }
    public bool Selected { get; set; }
}

public static class Palette
{
    public static readonly IBrush Plants = Gradient("#5BBF4A", "#2E7D32");
    public static readonly IBrush Zombies = Gradient("#9575CD", "#5E35B1");
    public static readonly IBrush Gnomes = Gradient("#FFB74D", "#EF6C00");
    public static readonly IBrush Maps = Gradient("#4DD0E1", "#00838F");
    public static readonly IBrush Props = Gradient("#90A4AE", "#455A64");
    public static readonly IBrush Effects = Gradient("#FF8A65", "#D84315");
    public static readonly IBrush Neutral = Gradient("#78909C", "#37474F");

    public static IBrush Team(string team) => team switch
    {
        "Plants" => Plants,
        "Zombies" => Zombies,
        "Gnomes" => Gnomes,
        _ => Neutral,
    };

    private static IBrush Gradient(string from, string to) => new LinearGradientBrush
    {
        StartPoint = new Avalonia.RelativePoint(0, 0, Avalonia.RelativeUnit.Relative),
        EndPoint = new Avalonia.RelativePoint(1, 1, Avalonia.RelativeUnit.Relative),
        GradientStops = { new GradientStop(Color.Parse(from), 0), new GradientStop(Color.Parse(to), 1) },
    };
}
