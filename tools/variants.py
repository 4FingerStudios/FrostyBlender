"""The maps inside a Garden Warfare level file.

One level file can hold several maps: GW1's Level_COOP_Suburbia is both Suburban Flats and Zomboss Estate. The
level's description says so (PVZUILevelDescriptionComponent.Variants: one title string per map), and its
categories name the switches: GameMode (Coop0, TeamDeathMatch1, ... the digit is the map) and TOD (Day, Night).
Every sub-world then lists, per switch, the options it loads for. Choosing map 1 at night keeps the sub-worlds
enabled for some mode ending in 1 and for Night.

A map in the list is the level's package plus '@' and a choice: '@1' (map 1), '@day', '@1-night'.
"""
import hashlib
import json
import os
import struct
from pathlib import Path

import gw2

VERSION = 'variants-v1'


# ---------------- reading the level descriptions ----------------
def _summary(data):
    doc = gw2.Ebx(data).document()
    out = {'variants': [], 'title': None, 'categories': {}}
    for obj in doc['objects']:
        kind = obj.get('$type')
        if kind == 'LevelDescriptionAsset':
            out['title'] = (obj.get('Description') or {}).get('Name')
            out['categories'] = {c.get('Category', ''): c.get('Mode', []) for c in obj.get('Categories', [])}
        elif kind == 'PVZUILevelDescriptionComponent':
            out['variants'] = list(obj.get('Variants') or [])
    return out


def _read_all(game):
    """{level name (lower case, e.g. levels/level_coop_asia/level_coop_asia): summary}. The descriptions all
    live in the Globals superbundle; the patch's delta bundles add the later levels."""
    found = {}
    toc = next((t for t in (Path(game) / 'Data/Win32').glob('*.toc') if t.stem.lower() == 'globals'), None)
    if toc is None:
        return found
    for entry, raw, decoded in gw2.bundles_with_patches(game, toc):
        try:
            assets, reader = gw2.bundle(raw)
        except (ValueError, struct.error, IndexError):
            continue
        for asset in assets:
            if asset['kind'] != 'ebx':
                break
            start = reader.at
            gw2.skip_asset(reader, asset['original_size'], decoded)
            name = asset['name'].lower()
            level, _, leaf = name.rpartition('/')
            if leaf not in ('description', 'description_win32'):
                continue
            r = gw2.Reader(raw)
            r.at = start
            try:
                data = r.take(asset['original_size']) if decoded else gw2.decompress(r, asset['original_size'])
                found[level] = _summary(data)
            except (ValueError, IndexError, struct.error, UnicodeError):
                continue
    return found


def level_key(relative):
    """'Win32/levels/level_coop_asia/level_coop_asia.toc' -> 'levels/level_coop_asia/level_coop_asia'."""
    rel = Path(relative).with_suffix('').as_posix().lower()
    return rel.split('/', 1)[1] if rel.startswith('win32/') else rel


def _cache_path(game):
    files = [root / 'Win32' / name for root in gw2.data_roots(Path(game)) for name in ('globals.toc', 'globals.sb',
                                                                                        'Globals.toc', 'Globals.sb')]
    files = sorted({f.resolve() for f in files if f.is_file()})
    key = hashlib.sha256(repr([VERSION, str(Path(game).resolve())] + [(str(t), t.stat().st_size, t.stat().st_mtime_ns)
                                                                       for t in files]).encode()).hexdigest()[:24]
    folder = Path(os.environ.get('AURA_IMPORT_CACHE') or Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'aura-frostbite')
    return folder / ('variants_%s.json' % key)


def describe_all(game):
    """Every level's description summary, cached on disk until the game's Globals change."""
    path = _cache_path(game)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    try:
        found = _read_all(game)
    except (ValueError, OSError, struct.error):
        return {}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found), encoding='utf-8')
    except OSError:
        pass
    return found


# ---------------- the list ----------------
def _night_category(categories):
    return next((name for name, options in categories.items() if any(o.lower() == 'night' for o in options)), None)


def entries(game, base, info, title_of):
    """The map entries of one level: base is its list entry, info what describe() found, title_of(sid) the
    game's text for a title id. A level with one map and no night version keeps a single entry."""
    if not info:
        return [base]
    variants = info.get('variants') or []
    night = _night_category(info.get('categories') or {}) is not None
    if len(variants) <= 1 and not night:
        return [base]
    out = []
    for index in range(max(1, len(variants))):
        name = (title_of(variants[index]) if index < len(variants) else None) or base['name']
        for dark in ((False, True) if night else (False,)):
            choice = ([str(index)] if len(variants) > 1 else []) + ((['night'] if dark else ['day']) if night else [])
            item = dict(base, name=name + (' Night' if dark else ''))
            if choice:
                item['package'] = base['package'] + '@' + '-'.join(choice)
            out.append(item)
    return out


def expand(game, maps):
    """The final map list: each level named from its description and split into its maps and night versions.
    maps are the discovered levels ({'name', 'package', ...}); their names stay where the description says
    nothing, and where two levels' descriptions give the same title (GW1's SuburbiaC calls itself Suburban
    Flats; it is Wall-nut Hills)."""
    import loc
    try:
        infos = describe_all(game)
    except Exception:  # noqa: BLE001 - the list must still come up without descriptions
        infos = {}

    def title_of(sid):
        try:
            return loc.text(game, sid) if sid else None
        except Exception:  # noqa: BLE001
            return None

    rows = []
    for base in maps:
        info = infos.get(level_key(base['package']))
        own = dict(base)
        described = title_of(info.get('title')) if info and len(info.get('variants') or []) <= 1 else None
        if info and len(info.get('variants') or []) == 1:
            described = title_of(info['variants'][0]) or described
        if described:
            own['name'] = described
        for item in entries(game, own, info, title_of):
            rows.append((item, base, bool(described)))
    counts = {}
    for item, _, _ in rows:
        counts[item['name']] = counts.get(item['name'], 0) + 1
    clashing = {id(base) for item, base, described in rows if described and counts[item['name']] > 1}
    for item, base, described in rows:
        if id(base) in clashing:
            item['name'] = base['name'] + (' Night' if item['name'].endswith(' Night') else '')
    return loc.name_maps([item for item, _, _ in rows])


def split(package):
    """'Win32/.../Level_X@1-night' -> ('Win32/.../Level_X', '1-night')."""
    base, _, choice = package.partition('@')
    return base, choice


# ---------------- choosing the sub-worlds ----------------
def chosen(choice):
    """(the map's digit or None, 'day' / 'night' / None) from a choice like '1-night'."""
    parts = [p for p in (choice or '').lower().split('-') if p]
    digit = next((p for p in parts if p.isdigit()), None)
    time = next((p for p in parts if p in ('day', 'night')), None)
    return digit, time


def included_subworlds(documents, choice):
    """Names (lower case) of the sub-worlds the choice loads, or None to keep everything. documents maps an
    EBX name to (asset, document) as the converters hold them."""
    digit, time = chosen(choice)
    if digit is None and time is None:
        return None
    by_name = {name.lower(): doc for name, (asset, doc) in documents.items()
               if asset.get('root_type') in ('LevelData', 'SubWorldData')}
    roots = [name for name, (asset, doc) in documents.items() if asset.get('root_type') == 'LevelData']

    def allowed(options):
        """One inclusion setting: the options it enables for its switch."""
        if not options:
            return False
        lowered = [o.lower() for o in options]
        if any(o in ('day', 'night') for o in lowered):
            return time is None or time in lowered
        if any(o.startswith('gamesim') or o.startswith('zdebug') for o in lowered):
            return False
        if digit is not None and all(o[-1:].isdigit() for o in lowered):
            return any(o.endswith(digit) for o in lowered)
        return True

    keep, queue = set(), [r.lower() for r in roots]
    while queue:
        name = queue.pop()
        if name in keep or name not in by_name:
            continue
        keep.add(name)
        objects = by_name[name]['objects']
        for obj in objects:
            if obj.get('$type') != 'SubWorldReferenceObjectData' or not obj.get('BundleName'):
                continue
            target = obj['BundleName'].lower()
            pointer = obj.get('InclusionSettings') or {}
            inclusion = objects[pointer['index']] if pointer.get('$pointer') == 1 else None
            settings = [objects[s['index']] for s in (inclusion or {}).get('Settings', []) if s.get('$pointer') == 1]
            if all(allowed(s.get('EnabledOptions') or []) for s in settings):
                queue.append(target)
    return keep
