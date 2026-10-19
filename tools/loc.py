"""English UI strings of Garden Warfare 1 and 2 (the game's UITextDatabase, Win32/loc/en).

The binary chunk (magic 0x00039000) holds, after its 8-byte header: the string count, the table and text
offsets, a 16-byte bank name, then one (hash, offset) pair per string and null-terminated text. A string ID
like ID_M_ASIA_TITLE is found by h = h * 33 + c over its characters, starting from 0xFFFFFFFF.
"""
import struct
from pathlib import Path

import gw2

MAGIC = 0x00039000
_cache = {}


def sid_hash(sid):
    h = 0xFFFFFFFF
    for c in sid.encode('ascii'):
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


def _chunks(toc):
    """(offset, size) of each chunk in a loc superbundle, read from its .sb."""
    return [(c['offset'], c['size']) for c in gw2.read_db(toc).get('chunks', []) or []]


def _read(sb, offset, size):
    with sb.open('rb') as stream:
        stream.seek(offset)
        data = stream.read(size)
    r, total = gw2.Reader(data), 0
    while r.at < len(data):
        decoded = r.number('>I') & 0xFFFFFF
        kind = r.number('<H')
        packed = r.number('>H') | ((kind >> 8 & 15) << 16)
        r.take(decoded if kind & 127 == 0 else packed)
        total += decoded
    return gw2.decompress(gw2.Reader(data), total)


def _table(data):
    _, _, count, table, text = struct.unpack_from('<5I', data, 0)
    pairs = struct.unpack_from('<%dI' % (count * 2), data, table + 8)
    base = text + 8
    strings = {}
    for i in range(count):
        at = base + pairs[2 * i + 1]
        end = data.find(b'\0', at)
        if at < len(data) and end > at:
            strings[pairs[2 * i]] = data[at:end].decode('latin-1')
    return strings


def strings(game):
    """{hash: text} of the English strings, patch first; {} when the game has none we can read."""
    game = Path(game)
    if game in _cache:
        return _cache[game]
    found = {}
    for root in gw2.data_roots(game):  # base first, then the patch overrides it
        toc = root / 'Win32/loc/en.toc'
        sb = toc.with_suffix('.sb')
        if not toc.is_file() or not sb.is_file():
            continue
        try:
            for offset, size in _chunks(toc):
                data = _read(sb, offset, size)
                if len(data) >= 36 and struct.unpack_from('<I', data)[0] == MAGIC:
                    found.update(_table(data))
        except (ValueError, struct.error, OSError):
            continue
    _cache[game] = found
    return found


MODES = {'coop': 'Garden Ops', 'rush': 'Gardens & Graveyards', 'herb': 'Herbal Assault', 'hub': 'Hub', 'endless': 'Endless'}


def level_title(game, stem):
    """The game's own name for a level file (level_coop_asia -> Zen Peak), or None. Its title IDs follow
    the level name: ID_M_COOP_SNOW_TITLE, ID_M_SNOW_TITLE, ID_M_PALMS, ID_M_SUBURBIA_C, ..."""
    core = stem.lower().removeprefix('level_').upper()
    parts = [p for p in core.split('_') if not p.isdigit()]
    rest = '_'.join(parts[1:] if parts and parts[0].lower() in MODES else parts)
    ids = [f'ID_M_{core}_TITLE', f'ID_M_{rest}_TITLE', f'ID_M_{rest}', f'ID_M_{core}']
    if len(rest) > 1 and rest[-2] != '_':
        ids.append(f'ID_M_{rest[:-1]}_{rest[-1]}')  # Suburbia2 / SuburbiaC: ID_M_SUBURBIA_2 / _C
    if parts and parts[0] == 'ENDLESS':
        ids.append('ID_M_ENDLESS_TITLE')  # the endless mode's one map: The Gnomiverse
    return text(game, *ids)


def mode(stem):
    """The game mode a level file is built for, from its name (level_rush_snow -> Gardens & Graveyards)."""
    first = stem.lower().removeprefix('level_').split('_')[0]
    return MODES.get(first)


def name_maps(maps):
    """Where two maps got the same name, add their mode: Driftwood Shores (Garden Ops)."""
    seen = {}
    for item in maps:
        seen.setdefault(item['name'], []).append(item)
    for name, items in seen.items():
        if len(items) > 1:
            for item in items:
                tag = mode(Path(item['package']).name)
                if tag:
                    item['name'] = f'{name} ({tag})'
    return maps


def text(game, *sids):
    """The first of these string IDs the game has, or None."""
    table = strings(game)
    for sid in sids:
        value = table.get(sid_hash(sid))
        if value:
            return value
    return None
