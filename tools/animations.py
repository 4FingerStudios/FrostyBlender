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


def _level_files(game):
    """GW1's level files, once each. The map list splits levels into maps and times of day ('…@1-night',
    see variants.py), but banks live in the level file itself."""
    import gw1
    seen = []
    for level in gw1.discover(game):
        base = level['package'].partition('@')[0]
        if base not in seen:
            seen.append(base)
    return seen


def level_for(game, kind):
    preferred = LEVELS[kind]
    if kind != 'gw1' or (Path(game) / 'Data' / (preferred + '.toc')).is_file():
        return preferred
    levels = _level_files(game)
    if not levels:
        raise ValueError('No GW1 levels found for animation banks')
    return max(levels, key=lambda level: (Path(game) / 'Data' / (level + '.sb')).stat().st_size)


def cache_dir(game, kind):
    level = 'all-levels' if kind == 'gw1' else level_for(game, kind)
    key = hashlib.sha256(('%s|%s|%s|banks-v2' % (Path(game).resolve(), kind, level)).encode()).hexdigest()[:16]
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
    if kind == 'gw1':
        # GW1 keeps each Garden Ops enemy's clips in the banks of the levels it appears in (Grave Digger
        # isn't in Mainstreet's), so every level is read; a bank shared by several levels is kept once.
        tocs = [Path(game) / 'Data' / (level + '.toc') for level in _level_files(game)]
    else:
        tocs = [Path(game) / 'Data' / (level_for(game, kind) + '.toc')]
    sizes, levels = {}, {}
    for toc in tocs:
        if not toc.is_file():
            continue
        held = levels.setdefault(toc.stem, [])
        try:
            for desc, raw, decoded in gw2.bundles_with_patches(Path(game), toc):
                try:
                    assets, r = gw2.bundle(raw)
                except (ValueError, struct.error):
                    continue
                for a in assets:
                    name = a['name'].split('/')[-1]
                    if a['kind'] == 'resource' and a['name'].lower().endswith('_antstate') and a['original_size'] > 4096:
                        if name not in held:
                            held.append(name)
                    if (a['kind'] == 'resource' and a['name'].lower().endswith('_antstate') and a['original_size'] > 4096
                            and a['original_size'] > sizes.get(name, 0)):
                        content = r.take(a['original_size']) if decoded else gw2.decompress(r, a['original_size'])
                        path = folder / (name + '.bin')
                        path.write_bytes(content)
                        sizes[name] = a['original_size']
                        if path not in out:
                            out.append(path)
                    elif decoded:
                        r.take(a['original_size'])
                    else:
                        _skip(r, a['original_size'])
        except (ValueError, OSError, struct.error) as error:  # one unreadable level must not lose the others
            print('Skipped animation banks in %s: %s' % (toc.stem, error), flush=True)
    (folder / 'levels.json').write_text(json.dumps({k: v for k, v in levels.items() if v}), encoding='utf-8')
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


def groups(game, kind):
    """Bank sets that are read together: GW1 one per level (its own banks plus the shared ones it carries),
    since levels give the same mapping objects different contents, and a build can mix PC and Xenon levels."""
    paths = banks(game, kind)
    listing = paths[0].parent / 'levels.json'
    if kind != 'gw1' or not listing.is_file():
        return {'': paths}
    by_name = {p.stem: p for p in paths}
    return {level: [by_name[n] for n in names if n in by_name]
            for level, names in json.loads(listing.read_text(encoding='utf-8')).items()}


def load(game, kind, group=None):
    sets = groups(game, kind)
    paths = sets.get(group or '') or sets.get(next(iter(sets)))
    return ant.Database([ant.read_bank(p.read_bytes()) for p in paths])


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


# Facial clips: GW1/GW2 keep small clips on a character's face bones (Soldier_Face_Idle, Rose_Idle_Face),
# BFN talk clips (..._NPC_Talk_Mouth_Open) that move the face with the body.
FACE_BONE = re.compile(r'face|eye|brow|lid|mouth|cheek|jaw|lip|nose|nostril|tongue|teeth|tooth', re.I)
BODY_BONE = re.compile(r'^(hips|pelvis|spine|trajectory|connect|root|reference)', re.I)
FACE_CLIP = re.compile(r'face|mouth|talk|lip|blink|viseme', re.I)
INDEX_VERSION = 2


def face_kind(name, ordered):
    """(face, face_only): a facial clip, and one that drives only face bones (laid over a body clip)."""
    bones = {n.rsplit('.', 1)[0] if n[-2:] in ('.q', '.t', '.s') else n for n, _, _ in ordered}
    bones.discard('AITrajectory')
    on_face = sum(1 for b in bones if FACE_BONE.search(b))
    face_only = bool(bones) and on_face >= 0.75 * len(bones) and not any(BODY_BONE.search(b) for b in bones)
    return face_only or bool(FACE_CLIP.search(name or '')), face_only


def sharing_rigs(db, anim, rig):
    """Other rigs with the face layout a face clip targets (GW1's zombie classes share one face)."""
    clip = db.clips_of.get(anim['__guid']) or {}
    target = db.get(clip.get('Target'))
    if target is None or target['__type'] != 'LayoutHierarchyAsset':
        return []
    layouts = set(target.get('LayoutAssets') or [])
    out = []
    for r in db.rigs:
        root = db.get((r.get('DofSetLists') or [None])[0])
        if r is rig or root is None or root['__guid'] == target['__guid']:
            continue
        if db._contains(root, target['__guid']) or (layouts and layouts <= set(root.get('LayoutAssets') or [])):
            out.append(r)
    return out


def discover(game):
    kind = game_kind(game)
    index = cache_dir(game, kind) / 'clips.json'
    if index.is_file():
        cached = json.loads(index.read_text(encoding='utf-8'))
        if cached.get('index') == INDEX_VERSION:
            return with_models(cached)
    characters = {}
    for group in groups(game, kind):
        db = load(game, kind, group)
        for anim in db.animations():
            if anim['__type'] != 'VbrAnimationAsset':
                continue
            try:
                ordered, rig, fps = db.channels(anim, kind)
            except (ValueError, KeyError, IndexError, TypeError):
                continue
            if not rig or not ordered:
                continue
            name = clean(db.clip_name(anim))
            face, face_only = face_kind(name, ordered)
            for owner in [rig] + (sharing_rigs(db, anim, rig) if face_only else []):
                entry = characters.setdefault(owner['__name'], {'rig': owner['__name'], 'name': character_of(owner['__name']),
                                                                'clips': [], 'seen': set()})
                if name.lower() in entry['seen']:
                    continue  # the same clip in both platforms' banks
                entry['seen'].add(name.lower())
                entry['clips'].append({'name': name, 'key': anim['__guid'], 'frames': anim.get('NumKeys', 0),
                                       'fps': fps, 'additive': bool(anim.get('Additive')), 'banks': group,
                                       'face': face, 'face_only': face_only})
    for entry in characters.values():
        entry.pop('seen')
    result = {'index': INDEX_VERSION, 'game': kind, 'level': 'all levels' if kind == 'gw1' else level_for(game, kind),
              'characters': sorted((c for c in characters.values() if len(c['clips']) >= 3), key=lambda c: c['name'].lower())}
    for c in result['characters']:
        c['clips'].sort(key=lambda x: x['name'].lower())
    index.write_text(json.dumps(result), encoding='utf-8')
    return with_models(result)


def export(game, items, out):
    kind = game_kind(game)
    dbs = {}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    written, errors = [], []
    for item in items:
        group = item.get('banks')
        if group not in dbs:
            dbs[group] = load(game, kind, group)
        db = dbs[group]
        anim = db.get(item['key'])
        if anim is None:
            errors.append({'clip': item.get('name'), 'error': 'Not in the animation banks'})
            continue
        try:
            clip = ant.clip_frames(db, anim, kind)
        except Exception as error:  # one bad clip must not lose the others
            errors.append({'clip': item.get('name'), 'error': str(error)})
            continue
        face, face_only = face_kind(clip['name'], db.channels(anim, kind)[0])
        clip.update(format='aura-frostbite-animation', version=1, name=clean(clip['name']), game=kind,
                    face=face, face_only=face_only)
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
