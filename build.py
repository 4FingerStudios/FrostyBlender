"""Build the release zip of the Frostbite Map Importer from this repository.

    python build.py [--blender PATH_TO_BLENDER_EXE] [--no-types] [--no-app] [--dev]

Needs Windows, Python 3, CMake and Visual Studio (C++ workload) to build the texture reader,
and Blender 4.2+ to pack the extension. Writes dist/v<version>/frostbite_importer-<version>-windows-x64.zip,
plus, when the .NET 8 SDK is installed, dist/v<version>/FrostyBlender-<version>-windows-x64.exe (the desktop
app, one self-contained file). A new app build for an add-on version that already has one goes to
dist/app-v<app version>/ instead, with a copy of the add-on zip. The zip needs nothing else on the player's PC: the import tools run on Blender's own Python, and
the zip carries them, the reader (static C++ runtime) and the BFN type name table.

The BFN key is never bundled; players enter their own in the add-on preferences.

Export for Unreal is dev only. Release builds leave it out of the add-on and the app; --dev keeps it and writes
everything to dist/dev-v<version>/ instead: the add-on with Export for Unreal, FrostyBlender-<app>-dev-windows-x64.exe
(Send to Unreal, no startup update checks) and FrostyBlender-Unreal-<version>.zip (the Unreal Engine 5 plugin).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ADDON = ROOT / 'frostbite_importer'
TOOLS = ROOT / 'tools'
READER = ROOT / 'reader'
APP = ROOT / 'app' / 'FrostyBlender'
UNREAL = ROOT / 'unreal_plugin' / 'FrostyBlender'
TYPES = ROOT / 'data' / 'bfn_types.json'
DIST = ROOT / 'dist'
# Export for Unreal and the unfinished Frosty mod writer, in --dev builds only.
DEV_ONLY = ('unreal_export.py', 'unreal_link.py', 'unreal', 'mod_export.py')
DEV_TOOLS = ('fbmod.py', 'mesh_mod.py')
SECRET = re.compile(r'(?i)aura_bfn_key\s*=\s*["\'][0-9a-f]{16,}')


def find_blender(given):
    if given:
        return Path(given)
    if os.environ.get('BLENDER'):
        return Path(os.environ['BLENDER'])
    found = shutil.which('blender')
    if found:
        return Path(found)
    roots = [Path('C:/Program Files/Blender Foundation'), Path('C:/Program Files (x86)/Steam/steamapps/common')]
    roots += [Path('%s:/SteamLibrary/steamapps/common' % d) for d in 'CDEFGHIJKLMNOPQRSTUVWXYZ']
    for root in roots:
        if root.is_dir():
            for exe in sorted(root.glob('Blender*/blender.exe'), reverse=True):
                return exe
    raise SystemExit('Blender not found: pass --blender or set BLENDER')


def build_reader(work):
    """aura_frostbite_reader.exe with the C++ runtime linked in, so no VC++ redistributable is needed."""
    build = work / 'reader_build'
    subprocess.run(['cmake', '-S', str(READER), '-B', str(build), '-A', 'x64',
                    '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded', '-DCMAKE_POLICY_DEFAULT_CMP0091=NEW'],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['cmake', '--build', str(build), '--config', 'Release', '-j', '8'], check=True,
                   stdout=subprocess.DEVNULL)
    exe = build / 'Release' / 'aura_frostbite_reader.exe'
    data = exe.read_bytes()
    for dll in (b'MSVCP140.dll', b'VCRUNTIME140.dll'):
        if dll in data:
            raise SystemExit('Reader still links %s dynamically' % dll.decode())
    return exe


def build_app(work, dev=False):
    """The FrostyBlender desktop app as one self-contained exe (no .NET install needed to run it)."""
    if not shutil.which('dotnet'):
        print('Skipping the app: the .NET 8 SDK (dotnet) is not installed', flush=True)
        return None
    out = work / 'app_publish'
    # Unreal=true compiles Send to Unreal in (the SDK recompiles when it changes, so dev and release share obj/).
    subprocess.run(['dotnet', 'publish', str(APP / 'FrostyBlender.csproj'), '-c', 'Release', '-o', str(out),
                    '-p:Unreal=%s' % str(dev).lower()], check=True, stdout=subprocess.DEVNULL)
    exe = out / 'FrostyBlender.exe'
    match = re.search(r'<Version>([^<]+)</Version>', (APP / 'FrostyBlender.csproj').read_text(encoding='utf-8'))
    app_version = match.group(1) if match else 'dev'
    name = 'FrostyBlender-%s%s-windows-x64.exe' % (app_version, '-dev' if dev else '')
    folder = DIST
    if not dev and any(p.name != name for p in DIST.glob('FrostyBlender-*-windows-x64.exe')):
        # This add-on version already shipped with another app: an app-only release gets its own folder
        # (dist/app-v1.8.0/), with a copy of the unchanged add-on zip so it holds the whole release.
        folder = DIST.parent / ('app-v' + app_version)
        folder.mkdir(parents=True, exist_ok=True)
        for addon_zip in DIST.glob('frostbite_importer-*-windows-x64.zip'):
            shutil.copy2(addon_zip, folder / addon_zip.name)
    target = folder / name
    shutil.copy2(exe, target)
    return target


def build_unreal_plugin(ver):
    """The Unreal plugin zip: unzipped into a project's Plugins folder it adds Tools > FrostyBlender."""
    target = DIST / ('FrostyBlender-Unreal-%s.zip' % ver)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(UNREAL.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                arc = 'FrostyBlender/' + path.relative_to(UNREAL).as_posix()
                if path.suffix == '.uplugin':
                    text = re.sub(r'"VersionName": "[^"]*"', '"VersionName": "%s"' % ver, path.read_text(encoding='utf-8'))
                    z.writestr(arc, text)
                else:
                    z.write(path, arc)
        # The same importer every export folder gets, under the name the plugin's menu imports.
        z.write(ADDON / 'unreal' / 'import_into_unreal.py', 'FrostyBlender/Content/Python/frostyblender_import.py')
        z.write(ROOT / 'LICENSE', 'FrostyBlender/LICENSE')
    return target


def version():
    match = re.search(r'^version = "([^"]+)"', (ADDON / 'blender_manifest.toml').read_text(encoding='utf-8'), re.M)
    return match.group(1)


def stage(work, reader, types, dev=False):
    out = work / 'frostbite_importer'
    shutil.copytree(ADDON, out, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', *(() if dev else DEV_ONLY)))
    shutil.copytree(TOOLS, out / 'tools', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', *(() if dev else DEV_TOOLS)))
    (out / 'bin').mkdir()
    shutil.copy2(reader, out / 'bin' / reader.name)
    if types:
        (out / 'data').mkdir()
        shutil.copy2(TYPES, out / 'data' / 'bfn_types.json')
    shutil.copy2(ROOT / 'LICENSE', out / 'LICENSE')
    shutil.copy2(ROOT / 'README.md', out / 'README.md')
    manifest = out / 'blender_manifest.toml'
    text = manifest.read_text(encoding='utf-8')
    if 'platforms' not in text:  # bcrypt, the game's Oodle DLL and the reader are Windows only
        text = text.replace('category = ', 'platforms = ["windows-x64"]\ncategory = ', 1)
    manifest.write_text(text, encoding='utf-8')
    for path in out.rglob('*'):
        if path.suffix in ('.py', '.toml', '.md', '.ps1') and SECRET.search(path.read_text(encoding='utf-8', errors='ignore')):
            raise SystemExit('Refusing to ship %s: it looks like it holds a BFN key' % path)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--blender', help='blender.exe (default: BLENDER, PATH or the usual install folders)')
    p.add_argument('--no-types', action='store_true', help='Do not bundle the BFN type name table')
    p.add_argument('--no-app', action='store_true', help='Do not build the FrostyBlender desktop app')
    p.add_argument('--dev', action='store_true',
                   help='Dev build with Export for Unreal, the Unreal plugin and Send to Unreal, in dist/dev-v<version>')
    a = p.parse_args()
    types = not a.no_types and TYPES.is_file()
    blender = find_blender(a.blender)
    ver = version()
    global DIST
    DIST = DIST / (('dev-v' if a.dev else 'v') + ver)  # one folder per release: dist/v1.10.0/
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / ('frostbite_importer-%s-windows-x64.zip' % ver)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        print('Building the reader...', flush=True)
        reader = build_reader(work)
        staged = stage(work, reader, types, a.dev)
        print('Packing the extension with', blender, flush=True)
        subprocess.run([str(blender), '--factory-startup', '--command', 'extension', 'build',
                        '--source-dir', str(staged), '--output-dir', str(work)], check=True, stdout=subprocess.DEVNULL)
        shutil.copy2(next(work.glob('frostbite_importer-*.zip')), target)
        app = None
        if not a.no_app and (APP / 'FrostyBlender.csproj').is_file():
            print('Building the FrostyBlender app...', flush=True)
            app = build_app(work, a.dev)
    with zipfile.ZipFile(target) as z:
        names = z.namelist()
    for need in ('bin/aura_frostbite_reader.exe', 'tools/gw2.py', 'tools/npcs.py', 'tools/editor_bridge.py', 'tools/icons.py', 'tools/effects.py', 'tools/explorer.py', 'tools/rig_models.json',
                 'tools/archive_parallel.py', 'tools/gw_water.py',
                 'app_link.py', 'addon_update.py', '__init__.py', 'LICENSE'):
        if need not in names:
            raise SystemExit('Release is missing ' + need)
    leaked = [n for n in names if n.split('/')[0] in DEV_ONLY or n.startswith('tools/') and n.split('/')[-1] in DEV_TOOLS]
    if leaked and not a.dev:
        raise SystemExit('Release holds dev-only files: %s' % ', '.join(leaked))
    print('Release:', target, '(%d files, %.1f MB)' % (len(names), target.stat().st_size / 1e6))
    if a.dev and UNREAL.is_dir():
        plugin = build_unreal_plugin(ver)
        print('Unreal: ', plugin, '(%.1f KB)' % (plugin.stat().st_size / 1e3))
    if app:
        print('App:    ', app, '(%.1f MB)' % (app.stat().st_size / 1e6))
    return 0


if __name__ == '__main__':
    sys.exit(main())
