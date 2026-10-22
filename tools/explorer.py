"""Game file explorer: every EBX file and resource in a Garden Warfare 1/2 or Battle for Neighborville install,
as a folder tree, with a look inside one file and an export of any selection.

    python explorer.py list --game GAME --out files.json
    python explorer.py inspect --game GAME --name PATH --reader READER --out info.json
    python explorer.py export --game GAME --items items.json --reader READER --dest FOLDER --out result.json
    python explorer.py sounds --game GAME --out sounds.json
    python explorer.py sound-export --game GAME --items names.json --dest FOLDER --out result.json
    python explorer.py text --game GAME [--language LANG] --out strings.json
    python explorer.py text-export --game GAME --dest FOLDER --out result.json

Exports: EBX files as readable JSON plus the raw data, textures (UI art included) as PNG, atlas flipbooks as
PNG, sounds (SoundWaveAsset) as WAV through audio.py, and other resources as their raw bytes with a fitting
extension. The game's UI text, every language, goes to CSV and JSON through loc.py. Meshes go to Blender through the
add-on's Props import instead. Battle for Neighborville EBX need the initfs key (AURA_BFN_KEY) and the type
name table (--names) to become JSON; without them they are saved raw. Game files are only read.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

import gw2
from cosmetics import game_kind

# Resource types, by the type id in the bundle manifests.
RESOURCE_TYPES = {
    0x6BDE20BA: 'Texture', 0xACD91FE8: 'Texture', 0x957C32B1: 'Atlas texture', 0x49B156D4: 'Mesh data',
    0x91043F65: 'Physics', 0xD070EED1: 'Animation track', 0x30B4A553: 'Occluder mesh', 0x51A3C853: 'Animation bank',
    0x70C5CB3E: 'Lighting', 0xC6CD3286: 'Lighting', 0x59CEEB57: 'Shader database', 0x36F3F2C0: 'Shader database',
    0x15E1F32E: 'Terrain decals', 0x1CA38E06: 'Terrain', 0x22FE8AC8: 'Terrain', 0xA23E75DB: 'Terrain',
    0x9D00966A: 'Font', 0x2D47A5FF: 'UI movie', 0x5C4954A6: 'UI data', 0x319D8CD0: 'Ragdoll', 0xC611F34A: 'Mesh emitter',
    0x0E2B02F7: 'Shader bytecode', 0xB2C465F6: 'Effect data', 0x41759364: 'Physics', 0x85EA8656: 'Cloth',
    0x85AC783D: 'Cloth mesh', 0x063994C5: 'Font', 0x7DD4CC89: 'Cloth settings', 0xB15AD3FD: 'Shader database',
}
TEXTURE_TYPES = {0x6BDE20BA, 0xACD91FE8}
ATLAS_TYPE = 0x957C32B1


def cache_root():
    root = Path(os.environ.get('AURA_IMPORT_CACHE') or Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'aura-frostbite')
    root.mkdir(parents=True, exist_ok=True)
    return root


def label(resource_type):
    return RESOURCE_TYPES.get(resource_type, 'Resource %08X' % (resource_type or 0))


# ---------------- listing ----------------
def _gw_listing(game):
    index = gw2.cached_dependency_index(Path(game))
    resources = {}
    for location in index['resource'].values():
        meta = location[3]
        resources.setdefault(meta['name'].lower(), meta)
    files, seen = [], set()
    for location in index['ebx'].values():
        meta = location[3]
        low = meta['name'].lower()
        if low in seen:
            continue
        seen.add(low)
        res = resources.get(low)
        files.append([meta['name'], label(res['resource_type']) if res else 'EBX', meta.get('original_size', 0)])
    for low, meta in resources.items():
        if low not in seen:
            seen.add(low)
            files.append([meta['name'], label(meta['resource_type']), meta.get('original_size', 0)])
    return files


def _bfn_listing(game):
    import bfn
    archive = bfn.Archive(game)
    files, seen, resources = [], set(), {}
    entries = []
    for sb in archive.sbs:
        if '/loc/' in sb.lower():
            continue
        for entry in archive.toc(sb)['bundles'].values():
            try:
                listed = archive.bundle(entry)
            except (ValueError, struct.error, IndexError):
                continue
            for a in listed:
                if a['kind'] == 'resource':
                    resources.setdefault(a['name'].lower(), a)
                elif a['kind'] == 'ebx':
                    entries.append(a)
    for a in entries:
        low = a['name'].lower()
        if low in seen:
            continue
        seen.add(low)
        res = resources.get(low)
        files.append([a['name'], label(res.get('resource_type')) if res else 'EBX', a.get('original_size', 0)])
    for low, a in resources.items():
        if low not in seen:
            seen.add(low)
            files.append([a['name'], label(a.get('resource_type')), a.get('original_size', 0)])
    return files


def listing(game):
    """[[path, type, size], ...] for every file, cached per install (rebuilt when the index changes)."""
    kind = game_kind(game)
    if kind == 'bfn':
        stamp = sorted((str(p), p.stat().st_mtime_ns) for p in (Path(game) / 'Data').glob('*.toc'))[:50]
    else:
        stamp = sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in (Path(game) / 'Data/Win32').rglob('*.toc'))
    key = hashlib.sha256(repr(['explorer-v2', str(Path(game).resolve()), stamp]).encode()).hexdigest()[:16]
    path = cache_root() / ('explorer_%s_%s.json' % (kind, key))
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    files = _bfn_listing(game) if kind == 'bfn' else _gw_listing(game)
    files.sort(key=lambda f: f[0].lower())
    result = {'game': kind, 'files': files}
    path.write_text(json.dumps(result), encoding='utf-8')
    return result


# ---------------- reading one file ----------------
class Source:
    """Reads an EBX file or resource from the install by name (base archives; the patch for BFN)."""

    def __init__(self, game):
        self.game, self.kind = Path(game), game_kind(game)
        if self.kind == 'bfn':
            import bfn
            self.archive = bfn.Archive(game)
            self._bfn = None
        else:
            index = gw2.cached_dependency_index(self.game)
            self.index = index
            self.ebx = {loc[3]['name'].lower(): loc for loc in index['ebx'].values()}
            self.guid_name = {guid: loc[3]['name'] for guid, loc in index['ebx'].items()}
            self.resources = {}
            for loc in index['resource'].values():
                self.resources.setdefault(loc[3]['name'].lower(), loc)

    def _bfn_assets(self):
        if self._bfn is None:
            self._bfn = {'ebx': {}, 'resource': {}, 'chunk': {}}
            for sb in self.archive.sbs:
                for entry in self.archive.toc(sb)['bundles'].values():
                    try:
                        listed = self.archive.bundle(entry)
                    except (ValueError, struct.error, IndexError):
                        continue
                    for a in listed:
                        if a['kind'] == 'chunk':
                            self._bfn['chunk'].setdefault(a['guid'], a['ref'])
                        else:
                            self._bfn[a['kind']].setdefault(a['name'].lower(), a)
        return self._bfn

    @staticmethod
    def _gw_read(location):
        path, offset, size, source = location
        with path.open('rb') as stream:
            stream.seek(offset)
            return gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])

    def ebx_bytes(self, name):
        if self.kind == 'bfn':
            asset = self._bfn_assets()['ebx'].get(name.lower())
            return self.archive.fetch(asset['ref']) if asset else None
        location = self.ebx.get(name.lower())
        return self._gw_read(location) if location else None

    def resource(self, name):
        """(bytes, resource type) or None."""
        if self.kind == 'bfn':
            asset = self._bfn_assets()['resource'].get(name.lower())
            return (self.archive.fetch(asset['ref']), asset.get('resource_type')) if asset else None
        location = self.resources.get(name.lower())
        return (self._gw_read(location), location[3].get('resource_type')) if location else None

    def chunk(self, guid):
        if self.kind == 'bfn':
            ref = self.archive.chunk(guid) or self._bfn_assets()['chunk'].get(guid)
            return self.archive.fetch(ref) if ref else None
        location = self.index['chunk'].get(guid)
        return self._gw_read(location) if location else None

    def document(self, name, data=None):
        """The decoded EBX document, or None when it can't be decoded here (BFN without its key)."""
        data = data if data is not None else self.ebx_bytes(name)
        if data is None:
            return None
        if self.kind != 'bfn':
            return gw2.Ebx(data).document()
        key, names = os.environ.get('AURA_BFN_KEY'), os.environ.get('AURA_BFN_TYPES')
        if not key or not names:
            return None
        import bfn
        if not hasattr(self, '_types'):
            self._types = bfn.load_types(self.game, bfn.key_from(None), names)
        return bfn.Ebx(data, self._types).document()

    def import_names(self, document):
        if self.kind == 'bfn':
            return []
        return sorted({self.guid_name.get(i['file'], i['file']) for i in document.get('imports', [])})


def _summary(obj, limit=40):
    """The root object's simple fields, for a quick look."""
    out = {}
    for key, value in obj.items():
        if key.startswith('$') or len(out) >= limit:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, dict) and value.get('$type') in ('Vec2', 'Vec3', 'Vec4'):
            out[key] = [round(value.get(c, 0), 4) for c in 'xyzw' if c in value]
        elif isinstance(value, dict) and '$pointer' in value:
            out[key] = {0: 'none', 1: 'internal object', 2: 'external file'}.get(value['$pointer'], '?')
        elif isinstance(value, dict) and '$resource' in value:
            out[key] = 'resource ' + str(value['$resource'])
        elif isinstance(value, list):
            out[key] = '%d entries' % len(value)
    return out


# ---------------- export ----------------
def _texture_pngs(game, kind, names, reader, dest, size):
    """PNGs for texture names, through the same path as the app's icons (handles PC and Xenon textures)."""
    import icons
    with tempfile.TemporaryDirectory(prefix='frosty_explorer_') as tmp:
        found = icons.decode(game, kind, sorted(names), reader, tmp, size)
        written = {}
        for name, png in found.items():
            target = Path(dest) / (name.replace('\\', '/') + '.png')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(png, target)
            written[name] = str(target)
        return written


def _atlas_png(source, name, data, target, limit=2048):
    import effects
    from convert import png
    chunk = source.chunk(data[16:32].hex())
    if chunk is None:
        raise ValueError('atlas pixels not found')
    image = effects.atlas_pixels(data, chunk, limit)
    target.parent.mkdir(parents=True, exist_ok=True)
    png(target, image.shape[1], image.shape[0], image.tobytes())


def _extension(resource_type, data):
    if data[:3] in (b'GFX', b'CFX'):
        return '.gfx'
    if data[:3] in (b'FWS', b'CWS', b'ZWS'):
        return '.swf'
    if data[:4] == b'DDS ':
        return '.dds'
    if data[:4] == b'RIFF':
        return '.wav'
    return '.res'


def export(game, items, reader, dest, size=2048):
    """Save each named file under `dest`, mirroring its game path. Returns what was written and what failed."""
    kind = game_kind(game)
    source = Source(game)
    dest = Path(dest)
    written, errors, textures = [], [], set()
    chunks = None
    for item in items:
        name = item['name'] if isinstance(item, dict) else item
        base = dest / name.replace('\\', '/')
        try:
            data = source.ebx_bytes(name)
            if data is not None:
                base.parent.mkdir(parents=True, exist_ok=True)
                base.with_suffix('.ebx').write_bytes(data)
                written.append(str(base.with_suffix('.ebx')))
                document = source.document(name, data)
                if document is not None:
                    base.with_suffix('.json').write_text(json.dumps(document, indent=1), encoding='utf-8')
                    written.append(str(base.with_suffix('.json')))
                if document is not None and document['root_type'] == 'SoundWaveAsset' and kind != 'bfn':
                    import audio
                    chunks = chunks or audio.Chunks(game)
                    written += audio.export_sound(source, chunks, name, document['objects'][0], dest)
            resource = source.resource(name)
            if resource is not None:
                content, rtype = resource
                if rtype in TEXTURE_TYPES:
                    textures.add(name.lower())
                elif rtype == ATLAS_TYPE:
                    _atlas_png(source, name, content, base.with_suffix('.png'), size)
                    written.append(str(base.with_suffix('.png')))
                else:
                    target = base.with_suffix(_extension(rtype, content))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    written.append(str(target))
            if data is None and resource is None:
                errors.append({'name': name, 'error': 'Not found in the game archives'})
        except Exception as error:  # one file must not stop the others
            errors.append({'name': name, 'error': str(error)})
    if textures:
        try:
            done = _texture_pngs(game, kind, textures, reader, dest, size)
            written += list(done.values())
            for name in sorted(textures - set(done)):
                errors.append({'name': name, 'error': 'Texture could not be decoded'})
        except Exception as error:
            errors.append({'name': '(textures)', 'error': str(error)})
    return {'written': written, 'errors': errors, 'dest': str(dest)}


def mesh_tables(game, names):
    """The MeshVariationDatabases that give these meshes their textures, for a Props import into Blender."""
    kind = game_kind(game)
    if kind == 'bfn':
        return []
    import cosmetics
    index = gw2.cached_dependency_index(Path(game))
    guid_of = {loc[3]['name'].lower(): guid for guid, loc in index['ebx'].items()}
    tables = cosmetics.variation_index(game, kind)
    out = []
    for name in names:
        for table in tables.get(guid_of.get(name.lower(), ''), [])[:3]:
            if table not in out:
                out.append(table)
    return out


def inspect(game, name, reader):
    """Type, size, the root object's fields, the files it imports, and a preview PNG for textures."""
    kind = game_kind(game)
    source = Source(game)
    info = {'name': name, 'game': kind}
    data = source.ebx_bytes(name)
    if data is not None:
        info['ebx_bytes'] = len(data)
        document = source.document(name, data)
        if document is not None:
            info['root_type'] = document['root_type']
            info['objects'] = len(document['objects'])
            info['fields'] = _summary(document['objects'][0]) if document['objects'] else {}
            info['imports'] = source.import_names(document)[:200]
            if document['root_type'] == 'SoundWaveAsset' and kind != 'bfn':
                import audio
                info['sound'] = audio.describe(name, document['objects'][0], audio.Chunks(game))
        else:
            info['root_type'] = 'EBX (needs the BFN key in Blender to read)'
    resource = source.resource(name)
    if resource is not None:
        content, rtype = resource
        info['resource_type'] = label(rtype)
        info['resource_bytes'] = len(content)
        preview_dir = cache_root() / 'explorer_previews'
        target = preview_dir / (hashlib.sha1(('%s|%s' % (game, name)).encode()).hexdigest()[:20] + '.png')
        try:
            if target.is_file():
                info['preview'] = str(target)
            elif rtype in TEXTURE_TYPES:
                with tempfile.TemporaryDirectory(prefix='frosty_preview_') as tmp:
                    done = _texture_pngs(game, kind, {name.lower()}, reader, tmp, 512)
                    if done:
                        preview_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(next(iter(done.values())), target)
                        info['preview'] = str(target)
            elif rtype == ATLAS_TYPE:
                _atlas_png(source, name, content, target, 512)
                info['preview'] = str(target)
        except Exception as error:
            info['preview_error'] = str(error)
    if data is None and resource is None:
        raise ValueError('Not found in the game archives: ' + name)
    return info


# ---------------- sounds and text ----------------
def sounds(game):
    """Every SoundWaveAsset with its kind (Voice, Music, Effect), duration, channels, codec and rate."""
    import audio
    return audio.listing(game)


def export_sounds(game, names, dest):
    """Chosen sounds as WAV files under dest, one per variation."""
    import audio
    return audio.export(game, names, dest)


def text(game, language=None):
    """The UI strings of one language, with their ID names where known, for browsing."""
    import loc
    return loc.browse(game, language)


def export_text(game, dest):
    """Every UI string, all languages, to strings.csv and strings.json under dest."""
    import loc
    return loc.export(game, dest)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('list', 'inspect', 'export', 'sounds', 'sound-export', 'text', 'text-export'))
    p.add_argument('--game', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--name')
    p.add_argument('--items')
    p.add_argument('--reader')
    p.add_argument('--dest')
    p.add_argument('--size', type=int, default=2048)
    p.add_argument('--language')
    a = p.parse_args()
    if a.action == 'list':
        result = listing(a.game)
    elif a.action == 'inspect':
        result = inspect(a.game, a.name, a.reader)
    elif a.action == 'sounds':
        result = sounds(a.game)
    elif a.action == 'sound-export':
        result = export_sounds(a.game, json.loads(Path(a.items).read_text(encoding='utf-8')), a.dest)
    elif a.action == 'text':
        result = text(a.game, a.language)
    elif a.action == 'text-export':
        result = export_text(a.game, a.dest)
    else:
        items = json.loads(Path(a.items).read_text(encoding='utf-8'))
        result = export(a.game, items, a.reader, a.dest, a.size)
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
