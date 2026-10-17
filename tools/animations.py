"""Character animations (ANT clips) of Garden Warfare 1 and 2 and Battle for Neighborville.

Each game keeps its clips in AntState banks: GW2 in one bank per level, GW1 split across a
level's banks (mappings in one, clip data in the shared gameplay banks), BFN in one large bank
per level (GenericData v2). One level whose banks hold every playable character is used per
game. Extracted banks and the clip index are cached, so later scans take seconds.

    python animations.py discover --game GAME --out clips.json
    python animations.py export --game GAME --items items.json --out DIR

The export writes one decoded clip per file (aura-frostbite-animation JSON) for the add-on to
put on a rigged character. Game files are only read.
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path

import ant
import gw2
from cosmetics import game_kind

# The level whose banks are read, and the level superbundle that holds them.
LEVELS = {'gw1': 'Win32/_pvz/Levels/Mainstreet/Level_COOP_Mainstreet/Level_COOP_Mainstreet',
          'gw2': 'Win32/levels/level_coop_asia/level_coop_asia',
          'bfn': 'level_picnic_root'}


def level_for(game, kind):
    preferred = LEVELS[kind]
    if kind != 'gw1' or (Path(game) / 'Data' / (preferred + '.toc')).is_file():
        return preferred
    import gw1
    levels = gw1.discover(game)
    if not levels:
        raise ValueError('No GW1 levels found for animation banks')
    return max(levels, key=lambda level: (Path(game) / 'Data' / (level['package'] + '.sb')).stat().st_size)['package']


def cache_dir(game, kind):
    key = hashlib.sha256(('%s|%s|%s|xenon-v1' % (Path(game).resolve(), kind, level_for(game, kind))).encode()).hexdigest()[:16]
    base = Path(os.environ.get('AURA_IMPORT_CACHE') or Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'aura-frostbite')
    return base / ('ant_%s_%s' % (kind, key))


def _skip(r, size):
    """Step over a GW1/GW2 compressed payload without decoding it."""
    left = size
    while left:
        decoded = struct.unpack_from('>I', r.data, r.at)[0] & 0xffffff
        kind = struct.unpack_from('<H', r.data, r.at + 4)[0]
        packed = struct.unpack_from('>H', r.data, r.at + 6)[0] | ((kind >> 8 & 15) << 16)
        r.at += 8 + (decoded if kind & 127 == 0 else packed)
        left -= decoded


def extract_banks(game, kind, folder):
    """Write the chosen level's AntState banks into `folder`; returns their paths."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    if kind == 'bfn':
        import bfn
        archive = bfn.Archive(game)
        for entry in bfn.level_bundles(archive, LEVELS[kind]):
            try:
                listed = archive.bundle(entry)
            except (ValueError, struct.error, IndexError):
                continue
            for a in listed:
                if a['kind'] == 'resource' and a['name'].lower().endswith('_antstate') and a['original_size'] > 4096:
                    path = folder / (a['name'].split('/')[-1] + '.bin')
                    path.write_bytes(archive.fetch(a['ref']))
                    out.append(path)
        return out
    toc = Path(game) / 'Data' / (level_for(game, kind) + '.toc')
    for desc, raw, decoded in gw2.bundles_with_patches(Path(game), toc):
        try:
            assets, r = gw2.bundle(raw)
        except (ValueError, struct.error):
            continue
        for a in assets:
            if a['kind'] == 'resource' and a['name'].lower().endswith('_antstate') and a['original_size'] > 4096:
                content = r.take(a['original_size']) if decoded else gw2.decompress(r, a['original_size'])
                path = folder / (a['name'].split('/')[-1] + '.bin')
                path.write_bytes(content)
                out.append(path)
            elif decoded:
                r.take(a['original_size'])
            else:
                _skip(r, a['original_size'])
    return out


def banks(game, kind):
    folder = cache_dir(game, kind)
    found = sorted(folder.glob('*.bin')) if folder.is_dir() else []
    if not found:
        found = extract_banks(game, kind, folder)
        if not found:
            raise ValueError('No animation banks found in ' + level_for(game, kind))
        print('Extracted %d animation banks' % len(found), flush=True)
    return found


def load(game, kind):
    return ant.Database([ant.read_bank(p.read_bytes()) for p in banks(game, kind)])


def character_of(rig_name):
    """Rig_P_PeaShooter_GW3 -> PeaShooter."""
    name = re.sub(r'^Rig_(?:P_|Z_|AI_)?', '', rig_name or '')
    return re.sub(r'_GW3$', '', name).replace('_', ' ') or 'Unknown'


def clean(name):
    return re.sub(r'\s+Anim$', '', name or '')


def with_models(result):
    """Name, per animated rig, the importable model it animates (rig_models.json: matched by bone names)."""
    try:
        table = json.loads((Path(__file__).with_name('rig_models.json')).read_text(encoding='utf-8')).get(result['game'], {})
    except (OSError, ValueError):
        table = {}
    for c in result['characters']:
        model = table.get(c['rig'])
        if model:
            c['model'] = {k: model[k] for k in ('section', 'id', 'name')}
    return result


def discover(game):
    kind = game_kind(game)
    index = cache_dir(game, kind) / 'clips.json'
    if index.is_file():
        return with_models(json.loads(index.read_text(encoding='utf-8')))
    db = load(game, kind)
    characters = {}
    for anim in db.animations():
        if anim['__type'] != 'VbrAnimationAsset':
            continue
        try:
            ordered, rig, fps = db.channels(anim, kind)
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        if not rig or not ordered:
            continue
        entry = characters.setdefault(rig['__name'], {'rig': rig['__name'], 'name': character_of(rig['__name']), 'clips': []})
        entry['clips'].append({'name': clean(db.clip_name(anim)), 'key': anim['__guid'], 'frames': anim.get('NumKeys', 0),
                               'fps': fps, 'additive': bool(anim.get('Additive'))})
    result = {'game': kind, 'level': level_for(game, kind),
              'characters': sorted((c for c in characters.values() if len(c['clips']) >= 3), key=lambda c: c['name'].lower())}
    for c in result['characters']:
        c['clips'].sort(key=lambda x: x['name'].lower())
    index.write_text(json.dumps(result), encoding='utf-8')
    return with_models(result)


def export(game, items, out):
    kind = game_kind(game)
    db = load(game, kind)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written, errors = [], []
    for item in items:
        anim = db.get(item['key'])
        if anim is None:
            errors.append({'clip': item.get('name'), 'error': 'Not in the animation banks'})
            continue
        try:
            clip = ant.clip_frames(db, anim, kind)
        except Exception as error:  # one bad clip must not lose the others
            errors.append({'clip': item.get('name'), 'error': str(error)})
            continue
        clip.update(format='aura-frostbite-animation', version=1, name=clean(clip['name']), game=kind)
        path = out / (re.sub(r'[^\w.-]+', '_', clip['name'])[:80] + '.json')
        path.write_text(json.dumps(clip), encoding='utf-8')
        written.append(str(path))
        print('Clip:', clip['name'], len(clip['frames']), 'frames', flush=True)
    result = {'clips': written, 'errors': errors}
    (out / 'clips.report.json').write_text(json.dumps(result, indent=1), encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('discover', 'export'))
    p.add_argument('--game', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--items', help='JSON list of {name, key} from discover')
    a = p.parse_args()
    if a.action == 'discover':
        Path(a.out).write_text(json.dumps(discover(a.game)), encoding='utf-8')
    else:
        result = export(a.game, json.loads(Path(a.items).read_text(encoding='utf-8')), a.out)
        print('Clips:', len(result['clips']), 'errors:', len(result['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
