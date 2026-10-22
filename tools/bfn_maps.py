"""Battle for Neighborville map names and the maps inside its level files.

The English strings are GW2's UITextDatabase format (tools/loc.py), kept as two chunks of the Win32/loc/en
superbundle (base and patch). The other chunks there are speech, so each chunk's first CAS block is probed
for the table's magic before the whole chunk is read.

BFN's level descriptions give no map names and no Variants, and its sub-worlds have no inclusion settings:
the game logic switches them. So each level's maps are listed here by the string IDs the game itself names
them with (ID_LEVEL_*, ID_H_SHUB_*; found in soloplay and UI data), and a choice is honoured by sub-world
name and, for a map that is one part of a bigger level, by cropping to that part:
- a region holds its free-roam map and a Team Vanquish map in one of its zones (Town Center: Pressure Pier).
  The Team Vanquish map is cropped to the spawns and points of interest of the region's gamemode_tvm.
- the social hub holds Giddy Park, Dave Manor and Zomboss HQ, each a sub-world socialspace_v03_<area>.
  An area keeps its own sub-world, drops the others, and the shared hub is cropped to around it.
- Town Center and the hub have day and night sub-worlds (worlds/day, worlds/night, season_00_default_night).

A map in the list is the level's package plus '@' and a choice, as in tools/variants.py: '@night',
'@tvm-day', '@davesmansion-night'.
"""
import re
import struct
from pathlib import Path

import loc

MAGIC = struct.pack('<I', loc.MAGIC)
_cache = {}

# Level leaf -> its maps: (choice, string ids, fallback name[, suffix]). '' is the level as a whole.
TIMES = (('day', ''), ('night', ' Night'))
LEVELS = {
    'dsub_region_towncenter': [('day', ['ID_TOWNCENTER'], 'Town Center'), ('night', ['ID_TOWNCENTER'], 'Town Center Night'),
                               ('tvm-day', ['ID_LEVEL_TC_SB_SUBREGION_SEASIDE'], 'Pressure Pier')],
    'dsub_region_cheesemines': [('', ['ID_LEVEL_CM_SB_REGION'], 'Mount Steep'),
                                ('tvm', ['ID_LEVEL_CM_SB_SUBREGION_MESA'], 'Rocky Flats')],
    'dsub_region_dreadwood': [('', ['ID_LEVEL_DW_SB_REGION'], 'Weirding Woods'),
                              ('tvm', ['ID_LEVEL_DW_SB_SUBREGION_FACTORY'], 'Z-Tech Factory')],
    'dsub_socialspace': [(area + '-' + time, [sid], fallback + suffix)
                         for area, sid, fallback in (('carnivalgardens', 'ID_H_SHUB_CARNIVALGARDENS', 'Giddy Park'),
                                                     ('davesmansion', 'ID_H_SHUB_DAVESMANSION', 'Dave Manor'),
                                                     ('ztechfactory', 'ID_H_SHUB_ZTECHFACTORY', 'Zomboss HQ'))
                         for time, suffix in TIMES],
    'dsub_tvm_cheesemines': [('', ['ID_LEVEL_CM_TVM'], 'Colossal Fossil')],
    'dsub_tvm_dreadwood': [('', ['ID_LEVEL_DW_TVM'], 'Ruiny Ruins')],
    'dsub_tvm_suburbs': [('', ['ID_LEVEL_TC_TVM'], 'Daisy Drive')],
    'dsub_tvm_zrefinery': [('', ['ID_LEVELNAME_ZREFINERY'], 'Z-Tech Oozevoir')],
    'dsub_rush_towncenter': [('', ['ID_LEVEL_TC_TT'], 'Peachy District')],
    'dsub_rush_shoreline': [('', ['ID_LEVEL_SL_TT'], 'Turning Point')],
    'dsub_rush_dreadwood': [('', ['ID_LEVEL_DW_TT'], 'Loggy Acres')],
    'dsub_rush_cheesemines': [('', ['ID_LEVEL_CM_TT'], 'Goopy Gully')],
    'dsub_rush_farm': [('', ['ID_LEVELNAME_FARM'], 'Preserve Pastures')],
    'dsub_rush_resort': [('', ['ID_LEVELNAME_RESORT'], 'Tourist Trap Island')],
    'dsub_4v4_superarena': [('', ['ID_LEVEL_FD_4V4'], 'Funderdome')],
    'dsub_4v4_battlearena_v2': [('', ['ID_LEVEL_FDV2_4V4'], 'Funderdome v2')],
}
# The hub's seasonal terrains, named after it: Giddy Park (Winter Terrain).
for _season in ('beach', 'summer', 'winter'):
    LEVELS['dsub_social_terrain_' + _season] = [('', ['ID_LEVEL_SOCIALHUB'], 'Giddy Park', ' (%s Terrain)' % _season.capitalize())]
# Not maps: the menu's root level (game-mode logic only), the splash screen and the podium.
HIDDEN = {'level_picnic_root', 'level_picnic_splash', 'dsub_podiumcelebration'}


# ---------------- strings ----------------
def _first_block(archive, ref):
    path = archive.cas_path(ref)
    head = archive.read(path, ref[3], 8)
    size = struct.unpack_from('>I', head)[0] & 0xffffff
    code = struct.unpack_from('<H', head, 4)[0]
    packed = struct.unpack_from('>H', head, 6)[0] | ((code >> 8 & 15) << 16)
    return archive.oodle.cas(archive.read(path, ref[3], 8 + (packed if code & 0x7f else size)))


def strings(archive):
    """{hash: text} of the English strings, patch over base; {} when they cannot be read."""
    if archive.game in _cache:
        return _cache[archive.game]
    found = {}
    for folder in ('Data', 'Patch'):
        toc = archive.game / folder / 'Win32/loc/en.toc'
        if not toc.is_file():
            continue
        try:
            for ref in archive.parse_toc(toc)['chunks'].values():
                if _first_block(archive, ref)[:4] == MAGIC:
                    found.update(loc._table(archive.fetch(ref)))
        except (ValueError, OSError, struct.error):
            continue
    _cache[archive.game] = found
    return found


def text(archive, *sids):
    table = strings(archive)
    for sid in sids:
        value = table.get(loc.sid_hash(sid))
        if value:
            return value.strip()
    return None


def presence(archive, leaf):
    """An unlisted level's name from its rich presence string: ID_RP_DSUB_FARM_RUSH 'In Turf Takeover on
    Preserve Pastures' -> Preserve Pastures."""
    parts = re.sub(r'^(level|dsub)_', '', leaf).upper().split('_')
    ids = ['ID_RP_DSUB_' + '_'.join(parts), 'ID_RP_DSUB_' + '_'.join(parts[1:] + parts[:1])]
    value = text(archive, *ids)
    return re.sub(r'^In (.+? on )?', '', value) if value else None


# ---------------- the list ----------------
def internal(leaf):
    return ' '.join(w.capitalize() for w in re.sub(r'^(level|dsub)_', '', leaf).split('_'))


def entries(archive, leaf, base):
    """The list entries of one level file."""
    if leaf in HIDDEN:
        return []
    listed = LEVELS.get(leaf)
    if not listed:
        name = presence(archive, leaf)
        return [dict(base, name=name) if name else base]
    out = []
    for choice, sids, fallback, *suffix in listed:
        name = (text(archive, *sids) or fallback) + ''.join(suffix)
        if choice.endswith('night') and not name.endswith(' Night'):
            name += ' Night'
        out.append(dict(base, name=name, package=base['package'] + ('@' + choice if choice else '')))
    return out


def title(game, leaf, choice):
    """The map's name for a level file and choice, for the converted scene."""
    import bfn
    try:
        archive = bfn.Archive(game)
        for item in entries(archive, leaf, {'name': internal(leaf), 'package': ''}):
            if item['package'].partition('@')[2] == (choice or ''):
                return item['name']
    except (ValueError, OSError):
        pass
    return None


# ---------------- choosing the sub-worlds ----------------
def chosen(choice):
    """(area or None, 'day' / 'night' / None) from a choice like 'davesmansion-night'."""
    parts = [p for p in (choice or '').lower().split('-') if p]
    time = next((p for p in parts if p in ('day', 'night')), None)
    area = next((p for p in parts if p not in ('day', 'night')), None)
    return area, time


SEASON = re.compile(r'^season(?:al)?_(\d+)_')


def keeps(leaf, siblings, choice):
    """Whether a sub-world (lower-case leaf name) loads for the choice. siblings are the leaf names of every
    sub-world in the level."""
    season = SEASON.match(leaf)
    if season and int(season.group(1)):
        return False  # the hub's later seasons each redress all of it; season 00 is the default
    area, time = chosen(choice)
    words = leaf.split('_')
    if 'sunset' in words:
        return False  # a third lighting setup of the hub
    if time:
        night = 'night' in words
        day = 'day' in words or leaf + '_night' in siblings
        if (night and time != 'night') or (day and time != 'day'):
            return False
    if area and leaf.startswith('socialspace_v03_'):
        return leaf == 'socialspace_v03_' + area
    return True


def anchor(leaf, area):
    """Whether a sub-world is the one a choice's area is cropped around."""
    return bool(area) and (leaf == area or leaf.endswith('_' + area) or leaf == 'gamemode_' + area)


CROP_MARGIN = 40.0


def bounds(points, margin=CROP_MARGIN):
    """(min x, min z, max x, max z) around the points' x and z, or None."""
    if not points:
        return None
    xs, zs = [p[0] for p in points], [p[1] for p in points]
    return min(xs) - margin, min(zs) - margin, max(xs) + margin, max(zs) + margin


def inside(box, x, z):
    return box[0] <= x <= box[2] and box[1] <= z <= box[3]
