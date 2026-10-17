# FrostyBlender

Bring maps, characters, customizations, NPCs, props and animations from **Plants vs. Zombies: Garden Warfare**, **Garden Warfare 2** and **Battle for Neighborville** into Blender. FrostyBlender is two parts that work together: a Blender add-on (the Frostbite Map Importer) that does the importing, and a desktop app for browsing everything and sending it to Blender in one click.

## Credits

- Made by **4FingerStudios**
- Plugin by **Zander/SillyDev**
- App assistance by **Spooky4576**
- Code base by **Brando/Physics**
- Effects idea by **V0e**
- Shader idea by **GalaxyMan2015**, **Window** and **Brawltendo**
- Special thanks to **Window**
- Thank you to our testers: **\_Crow**

## Good to know

- **Battle for Neighborville needs a key.** BFN's files are encrypted, and the key isn't included. Enter your own in the add-on preferences. Garden Warfare 1 and 2 need no setup at all.
- **No Frosty Editor needed.** The add-on comes with the table of BFN data names (taken from Frosty Editor's public data), so you don't need Frosty installed.

## What you need

- Windows 10 or 11 (64-bit)
- Blender 4.2 or newer
- The games installed on your PC (EA app, Origin or Steam). The add-on reads your own game files and never changes them.

Nothing else to install: the add-on brings its own tools and runs them on Blender's built-in Python.

## Install

Download both files from the [latest release](https://github.com/4FingerStudios/FrostyBlender/releases/latest):

| File | What it is |
| --- | --- |
| `frostbite_importer-…-windows-x64.zip` | The Blender add-on. Needed for everything. |
| `FrostyBlender-…-windows-x64.exe` | The desktop app (optional). One file, nothing to install. |

1. In Blender: **Edit → Preferences → Get Extensions → ⌄ (top right) → Install from Disk…** and pick the zip. Don't unzip it. If you're updating, restart Blender afterwards.
2. Open the add-on's preferences (**Edit → Preferences → Add-ons → Frostbite Map Importer**). Check that your games show a ✓ under **Game folders**. They're usually found automatically. If one shows ⚠, click its folder icon and pick the game's install folder (the folder that holds the game's `.exe`).

That's all you need for Garden Warfare 1 and 2.

### Battle for Neighborville

BFN's files are encrypted. Paste your BFN initfs key into **BFN key** in the add-on preferences. The key is not included with this add-on.

## Use it

Everything lives in the 3D Viewport sidebar (press **N**). Pick the game with the **GW1 / GW2 / BFN** buttons, then click the scan button. The first scan of a game can take up to a minute; after that it's quick.

| Tab | What it does |
| --- | --- |
| **Frostbite** | Whole maps. Pick one and click **Import map**. Big maps take a few minutes, and Blender stays usable while they convert. |
| **Customizations** | A character's cosmetics (heads, hats, face props, arms, bodies…). Leave **Rig to skeleton** on so everything shares one armature. |
| **NPCs** | Horde, bosses, turrets and vehicles with their costumes, props, weapons and abilities, plus ability objects such as the Chili Bean bomb, Potato Mine, Spikeweed, drones and teleporters. Everything comes rigged. **Spawn at 3D cursor** places them where you want. |
| **Props** | Single props and buildings from any map. |
| **Effects** | Explosions, smoke, sparks and muzzle flashes from GW1 and GW2, such as the Chili Bean blast. Each emitter becomes a Geometry Nodes particle object with its flipbook textures, colour and fade curves, plus the effect's lights. Press play to watch it; quads face the scene camera. Heat distortion, ribbons, turbulence and sounds are not ported. |
| **Game shaders** (optional) | Tick **Game shaders** before importing GW1/GW2 maps, characters, NPCs or props. Materials are then wired the way the games' own shaders read their textures, decoded from GW2's compiled shaders: ASM (occlusion, smoothness, metalness), ETT (emissive and tint masks), paint tints by UV quadrant, and the R x A normal maps. Off by default; BFN keeps the default materials. |
| **Animations** | Animation clips for a character, NPC or ability you imported. The tab names the model each set of clips plays on. Select its armature, pick clips and click **Import selected**. |

Tips:
- Import a character's body first, then add hats, props and weapons. Items you add later go onto the same armature, so the whole character animates together.
- **Texture size** (in the tabs) trades quality for speed. 1024 is a good default; 512 helps on low-memory PCs.
- Converted files are kept in **Documents\Frostbite Blender**, so you can delete old imports there to free space.

## FrostyBlender app

A desktop app for browsing everything in the games, in the style of Fortnite Porting. Pick **Maps**, **Characters**, **NPCs**, **Props** or **Animations**, choose GW1 / GW2 / BFN, click a tile, tick what you want and press **Send to Blender**.

1. Install the add-on (above) and open Blender once. The add-on tells the app where Blender, its tools and your game folders are.
2. Run `FrostyBlender-…-windows-x64.exe`. It needs add-on 1.9.1 or newer for icons and loading screens.
3. Keep Blender open while you send imports. The status bar shows when Blender is connected.

Maps and props show each map's loading screen, so you can see what you're porting. Characters, NPCs and animated characters show the games' own menu art: hero portraits, boss heads, turret cards and the icon of each cosmetic. Icons are made the first time you open a game or character (a few seconds) and kept, so they show instantly after that. Items the game has no art for keep a plain row.

Browsing works even when Blender is closed. Imports run in Blender exactly as if you'd pressed Import in the add-on's tab. The add-on only listens on this PC (`127.0.0.1`, port 24567), and you can turn the link off in its preferences under **Link to the FrostyBlender app**.

## Troubleshooting

- **"Set the … game folder"**: set that game's folder in the add-on preferences (the folder with the game's `.exe`).
- **BFN import fails right away**: check the BFN key in the preferences.
- **Import failed**: the status box names a log file in the job's folder under Documents\Frostbite Blender. Include it when you report a problem.
- **No icons or loading screens in the app**: install the newest add-on zip and restart Blender. The app's status bar says so when the add-on is too old.

## Building from source

Most people should download the ready-made zip from the **Releases** page. To build it yourself on Windows you need Python 3, CMake, Visual Studio with the C++ workload, and Blender 4.2+:

```
python build.py --blender "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
```

This writes `dist/frostbite_importer-<version>-windows-x64.zip`, and, if the .NET 8 SDK is installed, `dist/FrostyBlender-<version>-windows-x64.exe` (skip it with `--no-app`). Add `--no-types` to leave the BFN data name table out of the zip; BFN players would then need Frosty Editor to build it themselves.

| Folder | What's in it |
| --- | --- |
| `frostbite_importer/` | The Blender add-on (tabs, importer, rigging, animation, app link) |
| `app/FrostyBlender/` | The FrostyBlender desktop app (C#, Avalonia, .NET 8) |
| `tools/` | The import tools that read the game archives. They run on Blender's Python. |
| `reader/` | C++ texture reader (`aura_frostbite_reader.exe`), using GPL code from [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate) |
| `data/` | BFN class and field name table |

## License

GPL-3.0-or-later (see `LICENSE`). This repository is the full source of the release. This is a fan-made tool, not affiliated with or endorsed by Electronic Arts or PopCap. Plants vs. Zombies is a trademark of Electronic Arts Inc.

## Community

<p align="center">
  <a href="https://discord.gg/Vh7cgGzUyB"><img src="https://img.shields.io/badge/Join%20our%20Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Join our Discord"></a>
</p>

## Project stats

<p align="center">
  <a href="https://github.com/4FingerStudios/FrostyBlender/releases/latest"><img src="https://img.shields.io/github/v/release/4FingerStudios/FrostyBlender?style=for-the-badge&label=latest&color=2ea043" alt="Latest release"></a>
  <a href="https://github.com/4FingerStudios/FrostyBlender/releases"><img src="https://img.shields.io/github/downloads/4FingerStudios/FrostyBlender/total?style=for-the-badge&color=0a84ff" alt="Downloads"></a>
  <a href="https://github.com/4FingerStudios/FrostyBlender/stargazers"><img src="https://img.shields.io/github/stars/4FingerStudios/FrostyBlender?style=for-the-badge&color=f5c518" alt="Stars"></a>
  <a href="https://github.com/4FingerStudios/FrostyBlender/forks"><img src="https://img.shields.io/github/forks/4FingerStudios/FrostyBlender?style=for-the-badge" alt="Forks"></a>
</p>
<p align="center">
  <a href="https://github.com/4FingerStudios/FrostyBlender/issues"><img src="https://img.shields.io/github/issues/4FingerStudios/FrostyBlender?style=for-the-badge" alt="Open issues"></a>
  <a href="https://github.com/4FingerStudios/FrostyBlender/commits/main"><img src="https://img.shields.io/github/last-commit/4FingerStudios/FrostyBlender?style=for-the-badge" alt="Last commit"></a>
  <img src="https://img.shields.io/github/repo-size/4FingerStudios/FrostyBlender?style=for-the-badge" alt="Repo size">
  <img src="https://img.shields.io/github/languages/top/4FingerStudios/FrostyBlender?style=for-the-badge" alt="Top language">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/4FingerStudios/FrostyBlender?style=for-the-badge" alt="License"></a>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/Blender-4.2%2B-F5792A?style=for-the-badge&logo=blender&logoColor=white" alt="Blender 4.2+">
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?style=for-the-badge" alt="Windows 10 | 11">
  <img src="https://img.shields.io/badge/Games-GW1%20%7C%20GW2%20%7C%20BFN-7cb342?style=for-the-badge" alt="GW1 | GW2 | BFN">
</p>

<p align="center">
  <a href="https://star-history.com/#4FingerStudios/FrostyBlender&Date"><img src="https://api.star-history.com/svg?repos=4FingerStudios/FrostyBlender&type=Date" alt="Star history" width="600"></a>
</p>
