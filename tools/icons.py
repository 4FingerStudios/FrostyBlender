"""Game UI icons for characters, NPCs and cosmetics, as PNG files for the FrostyBlender app.

Each game ships its menu art as plain TextureAssets. Maps use their loading screens (GW1 also its
map cards; GW2's loading screens are atlases, cropped to the 16:9 picture). Cosmetics: GW2 in ui/assets/customize/<character>/<slot>/
named like the cosmetic, GW1 in _pvz/ui/textures/customize/<slot>/<character>/<item>, BFN in
ui/.../automatedassets/customization/icon_<character>_<slot>_<item>_(small|large). Portraits come
from the character icons, boss heads, cards and character-select art. Icons are matched by name,
read with their texture chunks (BFN needs no key for this: only the texture resources are read),
decoded by the reader and written as PNGs that later requests reuse.

    python icons.py --game GAME --requests REQUESTS.json --reader READER --out DIR --result RESULT.json

REQUESTS.json: {"portraits": [{"key", "id", "name"}], "items": [{"key", "character", "slot", "name", "bundle"}],
                "maps": [{"key", "package", "name"}]}
RESULT.json:   {"icons": {key: png path or null}}
"""
import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import gw2
from cosmetics import game_kind

PORTRAITS = {
    'gw1': ['_pvz/ui/textures/cardpacks/', '_pvz/ui/textures/cards/consumable/', '_pvz/ui/textures/cards/card_'],
    'gw2': ['ui/assets/charactericons/', 'ui/assets/bossheads/', 'ui/assets/hud/consumables/card_images/',
            'ui/assets/hud/consumables/aispawner_thumbnails/'],
    'bfn': ['ui/textures/textureasset/characterselect/classicons/', 'ui/textures/textureasset/automatedassets/opsbosses/',
            'ui/textures/textureasset/gw2/gw2hud/consumables/',
            'ui/textures/textureasset/automatedassets/customization/'],
}
MAPS = {
    'gw1': ['_pvz/ui/assets/loadscreen_', '_pvz/ui/textures/levels/level_'],
    'gw2': ['_pvz/ui/assets/loadscreen_'],
    'bfn': ['ui/textures/textureasset/loading/'],
}
# Words in map package names that name the mode, not the place.
MAP_MODES = {'level', 'dsub', 'coop', 'rush', 'tdm', 'herb', 'hub', 'fe', 'endless', 'cp', 'tvm', 'tvmlite', 'region',
             'social', 'terrain', '4v4', 'v2', 'root', 'soloplay', 'loadscreen', 'loadingscreen', 'screen', 'mixed'}
# Places whose loading screen goes by another name.
MAP_ALIASES = {'zombossfactory': ['factory'], 'suburbia': ['suburbs'], 'suburbia2': ['suburbs', 'suburbia'],
               'suburbiac': ['suburbs', 'suburbia'], 'resort': ['touristtrap'], 'zrefinery': ['oozevoir'],
               'socialspace': ['shub', 'socialhub'], 'picnic': ['shub', 'socialhub'], 'cheesemines': ['cheesedmines'],
               'superarena': ['battlearena']}
MODE_ALIASES = {'region': ['sandbox'], 'social': ['shub', 'socialhub']}
ITEMS = {
    'gw1': ['_pvz/ui/textures/customize/', '_pvz/ui/textures/cards/'],
    'gw2': ['ui/assets/customize/'],
    'bfn': ['ui/textures/textureasset/automatedassets/customization/'],
}
NOISE = {'icon', 'small', 'large', 'lrg', 'sml', 'ui', 'card'}
# Colour variants share their base item's icon in GW1.
VARIANTS = ('diamond', 'silver', 'gold', 'gem', 'royal', 'gummy', 'bling', 'shiny')
# NPCs whose menu art goes by another name.
PORTRAIT_ALIASES = {
    'bambooshoot': ['bamboo'], 'peashooter_turret_fire': ['firepea'], 'peashooter_turret_ice': ['icepea'],
    'peashooter_turret_gatling': ['gatling'], 'peashooter_turret_repeater': ['repeater'],
    'peashooter_turret_cannon': ['peacannon'], 'gargantuanimp': ['imp'], 'allstarimp': ['imp'], 'yeti_imp': ['imp'],
    'gargantuan': ['gargantuar'], 'mech_zombie': ['mechzombie'], 'peagatling': ['gatling'], 'simpleimp': ['imp'],
    'browncoat': ['browncoat'], 'tvhead': ['tvhead'], 'marygold': ['marigold'], 'weedplant': ['weed'],
    'gnomebomb': ['gnome'], 'gnomegun': ['gnome'], 'gnomemallet': ['gnome'], 'gnomeshield': ['gnome'], 'gigagnome': ['gnome'],
    'mechgargantuar': ['mechgargantuar', 'gargantuar'], 'treasureyeti': ['yeti'], 'yetiimp': ['imp'],
}
ICON_SIZE = 256


def norm(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def cache_root():
    return Path(os.environ.get('AURA_IMPORT_CACHE') or Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'aura-frostbite')


# ---------------- the UI texture names of a game ----------------
def ui_index(game, kind):
    """Names of the UI textures under the portrait and item folders, cached per install."""
    key = hashlib.sha256(('%s|%s|icons4' % (Path(game).resolve(), kind)).encode()).hexdigest()[:16]
    path = cache_root() / ('ui_icons_%s_%s.json' % (kind, key))
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))
    prefixes = tuple(PORTRAITS[kind] + ITEMS[kind] + MAPS[kind])
    if kind == 'bfn':
        import bfn
        archive = bfn.Archive(game)
        names = set()
        for sb in archive.sbs:
            for entry in archive.toc(sb)['bundles'].values():
                try:
                    listed = archive.bundle(entry)
                except (ValueError, struct.error, IndexError):
                    continue
                names.update(a['name'].lower() for a in listed
                             if a['kind'] == 'resource' and a.get('resource_type') == bfn.TEXTURE_TYPE
                             and a['name'].lower().startswith(prefixes))
    else:
        index = gw2.cached_dependency_index(Path(game))
        names = {loc[3]['name'].lower() for loc in index['ebx'].values() if loc[3]['name'].lower().startswith(prefixes)}
    result = sorted(names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding='utf-8')
    return result


# ---------------- matching ----------------
def _leaf(name):
    return name.rsplit('/', 1)[-1]


def portrait(names, kind, character_id, character_name=''):
    keys = {k for k in (norm(character_id), norm(character_name)) if len(k) >= 3}
    keys |= {k[:-6] for k in keys if k.endswith('zombie') and len(k) > 9}  # GW1: browncoatzombie -> browncoat
    aliases = {norm(a) for a in PORTRAIT_ALIASES.get(character_id.lower(), [])}
    for prefix in PORTRAITS[kind]:
        found = []
        for name in names:
            if not name.startswith(prefix):
                continue
            leaf = _leaf(name)
            if prefix.endswith('/customization/'):  # BFN: a hero's default skin icon
                if not any(leaf.startswith('icon_%s_skin_default' % k) for k in (character_id.lower(), norm(character_id))):
                    continue
            elif not any(k in norm(leaf) for k in keys):
                continue
            found.append(name)
        if not found and aliases and not prefix.endswith('/customization/'):
            found = [n for n in names if n.startswith(prefix) and any(a in norm(_leaf(n)) for a in aliases)]
        if found:
            found.sort(key=lambda n: ('default' not in _leaf(n), 'large' not in _leaf(n) and 'lrg' not in _leaf(n),
                                      len(_leaf(n)), n))
            return found[0]
    return None


def map_candidates(names, kind, package, map_name=''):
    """Loading-screen textures for a map, best first: the place must match; mode words, real
    loading screens and lower image numbers (_i1 before _i3) rank higher."""
    leaf = (package or '').rstrip('/').rsplit('/', 1)[-1].lower()
    tokens = [t for t in re.split(r'[^a-z0-9]+', leaf) if t and not t.isdigit()]
    places = [t for t in tokens if t not in MAP_MODES]
    modes = [t for t in tokens if t in MAP_MODES]
    if not places:  # level_fe_hub: the whole name is the place
        places = [norm(re.sub(r'^level_', '', leaf))]
    keys = [[norm(place)] + [norm(a) for a in MAP_ALIASES.get(place, [])] for place in places]
    mode_keys = [[norm(m)] + MODE_ALIASES.get(m, []) for m in modes if m not in ('level', 'dsub')]
    scored = []
    for name in names:
        if not name.startswith(tuple(MAPS[kind])):
            continue
        text = norm(_leaf(name)) + norm(name.rsplit('/', 2)[-2])
        hits = sum(1 for options in keys if any(k and k in text for k in options))
        if not hits:
            continue
        score = hits * 10 + sum(1 for options in mode_keys if any(k in text for k in options))
        score += 2 if '/loadscreen_' in name else 0
        score -= 5 if 'soloplay' in name else 0
        scored.append((-score, name))
    scored.sort()
    return [n for score, n in scored][:8]  # later ones stand in when the best is only a UI strip


def _stem(bundle):
    leaf = (bundle or '').rsplit('/', 1)[-1].lower()
    return re.sub(r'_(?:vis|shield|att|(?:bonus)?health[a-z]*?)?(?:unlockasset|visualasset|attachableasset|attachment)(?:_\w+)?_bpb$|_bpb$', '', leaf)


def item_icon(names, kind, character, slot, item_name, bundle):
    char = norm(character)
    stem = _stem(bundle)
    key = norm(item_name)
    slots = {norm(slot), norm(slot).rstrip('s'), norm(slot) + 's'} - {''}
    keys = [key] + [key[:-len(v)] for v in VARIANTS if key.endswith(v) and len(key) > len(v) + 2]
    words = (item_name or '').split()
    keys += [norm(' '.join(words[:n])) for n in range(len(words) - 1, 0, -1)]  # Angelfood Blue -> Angelfood
    keys += [k[len(s):] for k in list(keys) for s in slots if k.startswith(s) and len(k) > len(s) + 2]
    best, best_score = None, 0
    for name in names:
        if not name.startswith(tuple(ITEMS[kind])) or char not in norm(name):
            continue
        leaf = _leaf(name)
        tokens = [t for t in leaf.split('_') if t and t not in NOISE]
        rest = [t for t in tokens if norm(t) != char and norm(t) not in slots and norm(t) != norm(character.replace('_', ''))]
        joined = norm(''.join(rest))
        if norm(leaf) == norm(stem):
            score = 100
        elif joined == key:
            score = 80
        elif joined in keys:
            score = 70
        elif key and norm(leaf).endswith(key) and len(key) >= 4:
            score = 40
        elif any(k and norm(leaf).endswith(k) and len(k) >= 4 for k in keys[1:]):
            score = 35
        else:
            continue
        if any(s in norm(name.rsplit('/', 1)[0]) or s in tokens for s in slots):
            score += 5
        if 'large' in leaf or 'lrg' in leaf:
            score += 2
        if score > best_score:
            best, best_score = name, score
    return best


# ---------------- reading and decoding ----------------
def _png(width, height, rgba):
    rows = b''.join(b'\0' + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 6)) + chunk(b'IEND', b''))


def png_name(name):
    return hashlib.sha1(name.encode()).hexdigest()[:20] + '.png'


def _export_gw(game, kind, names, output):
    """TextureAsset EBX plus their resources and chunks, the way cosmetics are exported."""
    index = gw2.cached_dependency_index(Path(game))
    by_name = {loc[3]['name'].lower(): loc for loc in index['ebx'].values()}
    inventory = {'profile': kind, 'game': str(game), 'package': 'icons', 'assets': [], 'errors': [],
                 'archive_policy': 'base-and-installed-patch'}
    resource_of = {}
    for name in names:
        location = by_name.get(name)
        if not location:
            continue
        path, offset, size, source = location
        try:
            with path.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
            document = gw2.Ebx(content).document()
        except (ValueError, OSError, struct.error, IndexError) as error:
            inventory['errors'].append({'name': name, 'stage': 'ebx', 'error': str(error)})
            continue
        rid = ((document['objects'][0] if document['objects'] else {}).get('Resource') or {}).get('$resource')
        if document['root_type'] != 'TextureAsset' or not rid:
            continue
        resource_of[name] = rid
        asset = dict(source, path='ebx/%s.bin' % source['sha1'], document='ebx/%s.json' % source['sha1'],
                     bytes=len(content), root_type=document['root_type'], guid=document['guid'])
        (output / asset['path']).parent.mkdir(parents=True, exist_ok=True)
        (output / asset['path']).write_bytes(content)
        (output / asset['document']).write_text(json.dumps(document), encoding='utf-8')
        inventory['assets'].append(asset)
    gw2.complete_dependencies(Path(game), output, inventory)
    if kind == 'gw1':
        import gw1
        gw1.finish(Path(game), output, inventory)
    else:
        gw2.complete_texture_chunks(Path(game), output, inventory)
    if True:
        # UI textures keep their data in bundle chunks the dependency index knows, not streamed ones.
        have = {a['guid'] for a in inventory['assets'] if a['kind'] == 'chunk'}
        for asset in [a for a in inventory['assets'] if a.get('resource_type') == 0x6bde20ba and 'bytes' in a]:
            guid = (output / asset['path']).read_bytes()[32:48].hex()
            location = index['chunk'].get(guid)
            if guid in have or not location:
                continue
            path, offset, size, source = location
            with path.open('rb') as stream:
                stream.seek(offset)
                data = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
            chunk_path = 'chunk/%s.bin' % guid
            (output / chunk_path).parent.mkdir(parents=True, exist_ok=True)
            (output / chunk_path).write_bytes(data)
            inventory['assets'].append({'kind': 'chunk', 'name': guid, 'guid': guid, 'path': chunk_path, 'bytes': len(data)})
            have.add(guid)
    (output / 'inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
    return resource_of


def _export_bfn(game, names, output):
    """BFN texture resources by name and their chunks: no EBX, so no initfs key is needed."""
    import bfn
    archive = bfn.Archive(game)
    wanted = set(names)
    inventory = {'profile': 'bfn', 'game': 'bfn', 'game_root': str(game), 'package': 'icons', 'assets': [], 'errors': []}
    resource_of, chunk_refs = {}, {}
    for folder in ('resource', 'chunk'):
        (output / folder).mkdir(parents=True, exist_ok=True)
    for sb in archive.sbs:
        if not wanted:
            break
        for entry in archive.toc(sb)['bundles'].values():
            if not wanted:
                break
            try:
                listed = archive.bundle(entry)
            except (ValueError, struct.error, IndexError):
                continue
            for asset in listed:
                if asset['kind'] == 'chunk':
                    chunk_refs.setdefault(asset['guid'], asset['ref'])
                    continue
                name = asset['name'].lower()
                if asset['kind'] != 'resource' or name not in wanted or asset.get('resource_type') != bfn.TEXTURE_TYPE:
                    continue
                wanted.discard(name)
                try:
                    header = archive.fetch(asset['ref'])
                    guid = header[32:48].hex()
                    ref = archive.chunk(guid) or chunk_refs.get(guid)
                    if not ref:
                        raise ValueError('Texture chunk not found')
                    data = archive.fetch(ref)
                    header = bfn.texture_header(header, len(data))
                except (ValueError, OSError, struct.error, IndexError) as error:
                    inventory['errors'].append({'name': name, 'stage': 'texture', 'error': str(error)})
                    continue
                item = {k: v for k, v in asset.items() if k not in ('ref', 'original_size')}
                item['path'] = 'resource/%s.bin' % asset['sha1']
                (output / item['path']).write_bytes(header)
                item['bytes'] = len(header)
                inventory['assets'].append(item)
                chunk_path = 'chunk/%s.bin' % guid
                (output / chunk_path).write_bytes(data)
                inventory['assets'].append({'kind': 'chunk', 'name': guid, 'guid': guid, 'path': chunk_path, 'bytes': len(data)})
                resource_of[name] = item['resource_id']
    (output / 'inventory.json').write_text(json.dumps(inventory), encoding='utf-8')
    return resource_of


def _crop_picture(width, height, rgba):
    """A GW2 loading screen atlas: the 16:9 picture on its left, without the UI pieces beside it."""
    keep = round(height * 16 / 9)
    if not 0.38 <= height / width < 0.5 or keep >= width:
        return width, height, rgba
    rows = b''.join(rgba[y * width * 4:y * width * 4 + keep * 4] for y in range(height))
    return keep, height, rows


def decode(game, kind, names, reader, out):
    """Write the PNG of each UI texture name not already in `out`; returns {name: png path}."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    done = {n: out / png_name(n) for n in names if (out / png_name(n)).is_file()}
    todo = sorted(set(names) - set(done))
    if not todo:
        return {n: str(p) for n, p in done.items()}
    with tempfile.TemporaryDirectory(prefix='frosty_icons_') as tmp:
        export, textures = Path(tmp) / 'export', Path(tmp) / 'textures'
        export.mkdir()
        resource_of = _export_bfn(game, todo, export) if kind == 'bfn' else _export_gw(game, kind, todo, export)
        if resource_of:
            env = dict(os.environ, AURA_IMPORT_TEXTURE_SIZE=str(ICON_SIZE))
            subprocess.run([str(reader), '--textures', str(export), str(textures)], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                decoded = json.loads((textures / 'textures.json').read_text(encoding='utf-8'))['textures']
            except (OSError, ValueError, KeyError):
                decoded = {}
            for name, rid in resource_of.items():
                entry = decoded.get(str(rid))
                if not entry:
                    continue
                raw = (textures / entry['path']).read_bytes()
                width, height = struct.unpack_from('<II', raw)
                pixels = raw[8:8 + width * height * 4]
                if '/loadscreen_' in name:
                    width, height, pixels = _crop_picture(width, height, pixels)
                (out / png_name(name)).write_bytes(_png(width, height, pixels))
                done[name] = out / png_name(name)
    return {n: str(p) for n, p in done.items()}


def run(game, requests, reader, out):
    kind = game_kind(game)
    names = ui_index(game, kind)
    chosen = {}
    for request in requests.get('portraits', []):
        chosen[request['key']] = portrait(names, kind, request.get('id', ''), request.get('name', ''))
    for request in requests.get('items', []):
        chosen[request['key']] = item_icon(names, kind, request.get('character', ''), request.get('slot', ''),
                                           request.get('name', ''), request.get('bundle', ''))
    candidates = {r['key']: map_candidates(names, kind, r.get('package', ''), r.get('name', ''))
                  for r in requests.get('maps', [])}
    wanted = {n for n in chosen.values() if n} | {n for found in candidates.values() for n in found}
    files = decode(game, kind, sorted(wanted), reader, out)
    result = {key: files.get(name) if name else None for key, name in chosen.items()}
    for key, found in candidates.items():
        # The first candidate shaped like a picture (UI strips and bars are much wider than tall).
        result[key] = next((files[n] for n in found if n in files and 0.38 <= _aspect(files[n]) <= 0.8), None)
    return {'icons': result}


def _aspect(png):
    with open(png, 'rb') as stream:
        width, height = struct.unpack('>II', stream.read(24)[16:24])
    return height / width if width else 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--game', required=True)
    p.add_argument('--requests', required=True)
    p.add_argument('--reader', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--result', required=True)
    a = p.parse_args()
    result = run(a.game, json.loads(Path(a.requests).read_text(encoding='utf-8')), a.reader, a.out)
    Path(a.result).write_text(json.dumps(result), encoding='utf-8')
    found = sum(1 for v in result['icons'].values() if v)
    print('Icons:', found, 'of', len(result['icons']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
