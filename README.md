# Frostbite Map Importer for Blender

**Two things players should know:**

* The BFN key isn’t included. Players type in their own; GW1 and GW2 need no setup at all.
* The table of BFN data names comes from Frosty Editor’s public data. It’s bundled so players don’t need Frosty. If you’d rather not share it, run `python build.py --no-types` in the public folder; BFN players would then need Frosty installed to build the table themselves.

Bring maps, characters, customizations, NPCs, props and animations from **Plants vs. Zombies: Garden Warfare**, **Garden Warfare 2** and **Battle for Neighborville** into Blender.

By **4FingerStudios** · Made By Brando · Plugin made By SillyDev

## What you need

- Windows 10 or 11 (64-bit)
- Blender 4.2 or newer
- The games installed on your PC (EA app, Origin or Steam). The add-on reads your own game files and never changes them.

Nothing else to install: the add-on brings its own tools and runs them on Blender's built-in Python.

## Install

1. Download `frostbite_importer-…-windows-x64.zip` from the **Releases** page. Don't unzip it.
2. In Blender: **Edit → Preferences → Get Extensions → ⌄ (top right) → Install from Disk…** and pick the zip.
3. Open the add-on's preferences (**Edit → Preferences → Add-ons → Frostbite Map Importer**). Check that your games show a ✓ under **Game folders**. They're usually found automatically. If one shows ⚠, click its folder icon and pick the game's install folder (the folder that holds the game's `.exe`).

That's all you need for Garden Warfare 1 and 2.

### Battle for Neighborville

BFN's files are encrypted. Paste your BFN initfs key into **BFN key** in the add-on preferences. The key is not included with this add-on.

## Use it

Everything lives in the 3D Viewport sidebar (press **N**). Pick the game with the **GW1 / GW2 / BFN** buttons, then click the scan button. The first scan of a game can take up to a minute; after that it's quick.

| Tab | What it does |
| --- | --- |
| **Frostbite** | Whole maps. Pick one and click **Import map**. Big maps take a few minutes, and Blender stays usable while they convert. |
| **Customizations** | A character's cosmetics (heads, hats, face props, arms, bodies…). Leave **Rig to skeleton** on so everything shares one armature. |
| **NPCs** | Horde, bosses, turrets and vehicles with their costumes, props, weapons and abilities. **Spawn at 3D cursor** places them where you want. |
| **Props** | Single props and buildings from any map. |
| **Animations** | Animation clips for a character or NPC you imported. Select its armature, pick clips and click **Import selected**. |

Tips:
- Import a character's body first, then add hats, props and weapons. Items you add later go onto the same armature, so the whole character animates together.
- **Texture size** (in the tabs) trades quality for speed. 1024 is a good default; 512 helps on low-memory PCs.
- Converted files are kept in **Documents\Frostbite Blender**, so you can delete old imports there to free space.

## Troubleshooting

- **"Set the … game folder"**: set that game's folder in the add-on preferences (the folder with the game's `.exe`).
- **BFN import fails right away**: check the BFN key in the preferences.
- **Import failed**: the status box names a log file in the job's folder under Documents\Frostbite Blender. Include it when you report a problem.

## Building from source

Most people should download the ready-made zip from the **Releases** page. To build it yourself on Windows you need Python 3, CMake, Visual Studio with the C++ workload, and Blender 4.2+:

```
python build.py --blender "C:\Program Files\Blender Foundation\Blender 4.2\blender.exe"
```

This writes `dist/frostbite_importer-<version>-windows-x64.zip`.

| Folder | What's in it |
| --- | --- |
| `frostbite_importer/` | The Blender add-on (tabs, importer, rigging, animation) |
| `tools/` | The import tools that read the game archives. They run on Blender's Python. |
| `reader/` | C++ texture reader (`aura_frostbite_reader.exe`), using GPL code from [ReSkate](https://github.com/Dingo-Shenanigans/ReSkate) |
| `data/` | BFN class and field name table |

## License

GPL-3.0-or-later (see `LICENSE`). This repository is the full source of the release. This is a fan-made tool, not affiliated with or endorsed by Electronic Arts or PopCap. Plants vs. Zombies is a trademark of Electronic Arts Inc.
