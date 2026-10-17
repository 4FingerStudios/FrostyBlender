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
        try
        {
            BuildAvaloniaApp().StartWithClassicDesktopLifetime(args);
        }
        catch (Exception error)
        {
            // A crash leaves %LOCALAPPDATA%\FrostyBlender\crash.log to attach to a bug report.
            try
            {
                System.IO.Directory.CreateDirectory(Services.BlenderLink.Folder);
                System.IO.File.WriteAllText(System.IO.Path.Combine(Services.BlenderLink.Folder, "crash.log"),
                    $"{DateTime.Now:u}{Environment.NewLine}{error}");
            }
            catch (Exception) { }
            throw;
        }
    }

    public static AppBuilder BuildAvaloniaApp() =>
        AppBuilder.Configure<App>().UsePlatformDetect().WithInterFont().LogToTrace();
}
