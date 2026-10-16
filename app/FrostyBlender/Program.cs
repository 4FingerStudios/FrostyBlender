using System;
using Avalonia;

namespace FrostyBlender;

internal static class Program
{
    /// <summary>Optional start view: --section Characters --game GW2 --select peashooter.</summary>
    public static string[] Args { get; private set; } = Array.Empty<string>();

    [STAThread]
    public static void Main(string[] args)
    {
        Args = args;
        BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);
    }

    public static AppBuilder BuildAvaloniaApp() =>
        AppBuilder.Configure<App>().UsePlatformDetect().WithInterFont().LogToTrace();
}
