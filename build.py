"""Build the release zip of the Frostbite Map Importer from this repository.

    python build.py [--blender PATH_TO_BLENDER_EXE] [--no-types] [--no-app]

Needs Windows, Python 3, CMake and Visual Studio (C++ workload) to build the texture reader,
and Blender 4.2+ to pack the extension. Writes dist/v<version>/frostbite_importer-<version>-windows-x64.zip,
plus, when the .NET 8 SDK is installed, dist/v<version>/FrostyBlender-<version>-windows-x64.exe (the desktop
app, one self-contained file). The zip needs nothing else on the player's PC: the import tools run on Blender's own Python, and
the zip carries them, the reader (static C++ runtime) and the BFN type name table.

The BFN key is never bundled; players enter their own in the add-on preferences.
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
TYPES = ROOT / 'data' / 'bfn_types.json'
DIST = ROOT / 'dist'
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


def build_app(work):
    """The FrostyBlender desktop app as one self-contained exe (no .NET install needed to run it)."""
    if not shutil.which('dotnet'):
        print('Skipping the app: the .NET 8 SDK (dotnet) is not installed', flush=True)
        return None
    out = work / 'app_publish'
    subprocess.run(['dotnet', 'publish', str(APP / 'FrostyBlender.csproj'), '-c', 'Release', '-o', str(out)],
                   check=True, stdout=subprocess.DEVNULL)
    exe = out / 'FrostyBlender.exe'
    match = re.search(r'<Version>([^<]+)</Version>', (APP / 'FrostyBlender.csproj').read_text(encoding='utf-8'))
    target = DIST / ('FrostyBlender-%s-windows-x64.exe' % (match.group(1) if match else 'dev'))
    shutil.copy2(exe, target)
    return target


def version():
    match = re.search(r'^version = "([^"]+)"', (ADDON / 'blender_manifest.toml').read_text(encoding='utf-8'), re.M)
    return match.group(1)


def stage(work, reader, types):
    out = work / 'frostbite_importer'
    shutil.copytree(ADDON, out, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(TOOLS, out / 'tools', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
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
    a = p.parse_args()
    types = not a.no_types and TYPES.is_file()
    blender = find_blender(a.blender)
    ver = version()
    global DIST
    DIST = DIST / ('v' + ver)  # one folder per release: dist/v1.10.0/
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / ('frostbite_importer-%s-windows-x64.zip' % ver)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        print('Building the reader...', flush=True)
        reader = build_reader(work)
        staged = stage(work, reader, types)
        print('Packing the extension with', blender, flush=True)
        subprocess.run([str(blender), '--factory-startup', '--command', 'extension', 'build',
                        '--source-dir', str(staged), '--output-dir', str(work)], check=True, stdout=subprocess.DEVNULL)
        shutil.copy2(next(work.glob('frostbite_importer-*.zip')), target)
        app = None
        if not a.no_app and (APP / 'FrostyBlender.csproj').is_file():
            print('Building the FrostyBlender app...', flush=True)
            app = build_app(work)
    with zipfile.ZipFile(target) as z:
        names = z.namelist()
    for need in ('bin/aura_frostbite_reader.exe', 'tools/gw2.py', 'tools/npcs.py', 'tools/editor_bridge.py', 'tools/icons.py', 'tools/effects.py', 'tools/explorer.py', 'tools/rig_models.json',
                 'app_link.py', 'addon_update.py', '__init__.py', 'LICENSE'):
        if need not in names:
            raise SystemExit('Release is missing ' + need)
    print('Release:', target, '(%d files, %.1f MB)' % (len(names), target.stat().st_size / 1e6))
    if app:
        print('App:    ', app, '(%.1f MB)' % (app.stat().st_size / 1e6))
    return 0


if __name__ == '__main__':
    sys.exit(main())
