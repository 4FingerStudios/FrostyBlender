using System;
using System.Diagnostics;

namespace FrostyBlender.Services;

/// <summary>Why the app can't reach Blender: a short status line and what to do about it.</summary>
public static class BlenderDiagnosis
{
    /// <summary>The add-on rewrites link.json every 5 seconds while its link is on.</summary>
    private static readonly TimeSpan Fresh = TimeSpan.FromSeconds(30);

    public static (string Status, string Help) Describe(LinkInfo? link, int blenders, DateTime now)
    {
        if (blenders == 0)
            return link == null
                ? ("Add-on not set up yet", "Install the Frostbite Map Importer add-on in Blender (Edit › Preferences › Get Extensions › Install from Disk) and keep Blender open.")
                : ("Blender isn't open", "Open Blender. The app connects by itself.");
        if (link == null)
            return ("Blender is open, add-on not found", "Install the Frostbite Map Importer add-on in Blender (Edit › Preferences › Get Extensions › Install from Disk). If it's installed, make sure it's enabled, then restart Blender.");
        if (now - link.Updated < Fresh)
            return ("Blender is open, can't connect", $"The add-on is running but the app can't reach it on port {link.Port}. Allow FrostyBlender and Blender through your firewall or security software.");
        var others = blenders > 1 ? $"You have {blenders} copies of Blender open and only one can link, so close the others. " : "";
        return ("Blender is open, link is off", others + "Check that the add-on is enabled and that \"Link to the FrostyBlender app\" is ticked in its preferences. If it is, update the add-on to the latest version and restart Blender.");
    }

    public static int CountBlenders()
    {
        var processes = Process.GetProcessesByName("blender");
        foreach (var process in processes) process.Dispose();
        return processes.Length;
    }
}
