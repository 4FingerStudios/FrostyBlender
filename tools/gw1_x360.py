"""Garden Warfare 1 for Xbox 360: a disc's files plus its title update and DLC packs, read as one game.

A folder may be one disc copy (Default.xex, Data/Xenon/...) or a folder holding the disc and the packs,
each in a folder of its own with Data/Xenon (or Patch/Data/Xenon and DLCLevels/Data/Xenon). Every pack's
Data/layout.toc names its build ('head') and the disc build it patches ('base'): all of them patch the
disc itself, as delta bundles against its superbundles, with the DLC levels as superbundles they add in
full. Each pack holds every earlier pack's changes, so the disc is the base and the newest pack the patch,
like a PC install's Data and Update/Patch/Data. Older packs (and DLCLevels copies) are superseded.

The tools read PC layouts, so the folder is shown to them as a view: a cache folder whose Data/Win32 and
Update/Patch/Data/Win32 are directory junctions (symlinks elsewhere) to the disc's and newest pack's
Data/Xenon. Nothing is ever written into the game folders.
"""
import hashlib
import json
import os
from pathlib import Path

import gw2

MARKER = 'xenon-view.json'


def _layer(data):
    """{'data', 'head', 'base', 'added'} for a Data folder holding Xenon superbundles, else None."""
    if not (data / 'Xenon/Globals.toc').is_file() or not (data / 'layout.toc').is_file():
        return None
    try:
        layout = gw2.read_db(data / 'layout.toc')
    except (OSError, ValueError):
        return None
    if not isinstance(layout.get('head'), int):
        return None
    added = sorted(s['name'].split('/', 1)[-1] for s in layout.get('superBundles', []) if s.get('added'))
    return {'data': data, 'head': layout['head'], 'base': layout.get('base'), 'added': added}


def layers(folder):
    """(disc, packs): the disc's layer and the packs patching it, oldest first; (None, []) when the folder
    holds no Xbox 360 GW1 disc. A disc folder itself, or a folder of disc and pack folders (two levels deep)."""
    folder = Path(folder)
    if not folder.is_dir() or not (folder / 'Data/Xenon').is_dir() and not any(folder.glob('*/Data/Xenon')) \
            and not any(folder.glob('*/*/Data/Xenon')):
        return None, []
    found = []
    for data in [folder / 'Data'] + sorted(folder.glob('*/Data')) + sorted(folder.glob('*/*/Data')):
        layer = _layer(data)
        if layer:
            found.append(layer)
    discs = [l for l in found if l['base'] is None and (l['data'].parent / 'Default.xex').is_file()]
    if not discs:
        return None, []
    # The disc beside the folder itself first (a disc folder chosen directly), then the newest disc build.
    disc = next((l for l in discs if l['data'] == folder / 'Data'), max(discs, key=lambda l: l['head']))
    if disc['data'] == folder / 'Data':
        return disc, []  # a single disc folder: no packs
    packs = sorted((l for l in found if l['base'] == disc['head'] and l['head'] > disc['head']), key=lambda l: l['head'])
    return disc, packs


def is_x360(folder):
    return layers(folder)[0] is not None


def is_view(game):
    return (Path(game) / MARKER).is_file()


def _cache():
    return Path(os.environ.get('AURA_IMPORT_CACHE') or Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'aura-frostbite')


def _link(link, target):
    """Point a directory link at target, replacing an old link (never a real folder)."""
    target = Path(os.path.abspath(target))
    if os.path.lexists(link):
        try:
            if Path(os.readlink(link)) == target or Path(os.path.realpath(link)) == target:
                return
        except OSError:
            pass
        if not (os.path.islink(link) or getattr(Path(link), 'is_junction', lambda: False)()):
            raise ValueError('Xbox 360 view holds a real folder where a link belongs: %s' % link)
        os.rmdir(link)  # removes the link alone, not what it points to
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def view(folder):
    """The PC-layout view of an Xbox 360 GW1 folder (made or refreshed), for every tool to read."""
    folder = Path(os.path.abspath(folder))
    disc, packs = layers(folder)
    if disc is None:
        raise ValueError('Not an Xbox 360 Garden Warfare folder: %s' % folder)
    patch = packs[-1] if packs else None
    root = _cache() / 'gw1-xenon' / hashlib.sha256(str(folder).lower().encode()).hexdigest()[:16]
    _link(root / 'Data/Win32', disc['data'] / 'Xenon')
    patch_link = root / 'Update/Patch/Data/Win32'
    if patch:
        _link(patch_link, patch['data'] / 'Xenon')
    elif os.path.lexists(patch_link):
        os.rmdir(patch_link)
    # The view's own record: is_view finds it, and it says which folders the view shows.
    info = {'source': str(folder), 'disc': str(disc['data'].parent), 'disc_build': disc['head'],
            'patch': str(patch['data'].parent) if patch else None,
            'packs': [{'folder': str(l['data'].parent), 'build': l['head'], 'added': l['added']} for l in packs]}
    marker = root / MARKER
    if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != info:
        marker.write_text(json.dumps(info, indent=2), encoding='utf-8')
    return root


def game_root(game):
    """The folder the tools read for this game folder: its view when it is an Xbox 360 GW1 folder, else itself."""
    return view(game) if not is_view(game) and is_x360(game) else Path(game)


def source(game):
    """The view's record (see view), or None for any other folder."""
    try:
        return json.loads((Path(game) / MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Xbox 360 Garden Warfare folder: its disc, packs and view')
    parser.add_argument('folder')
    args = parser.parse_args()
    root = view(args.folder)
    print(json.dumps(dict(source(root), view=str(root)), indent=2))
