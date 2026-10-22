"""UI strings of Garden Warfare 1 and 2 and Battle for Neighborville (the game's UITextDatabase).

The binary chunk (magic 0x00039000) holds, after its 8-byte header: the string count, the table and text
offsets, the bank name, then one (hash, offset) pair per string and null-terminated text. A string ID
like ID_M_ASIA_TITLE is found by h = h * 33 + c over its characters, starting from 0xFFFFFFFF.

The histogram chunk (magic 0x00039001) turns the text bytes into characters: after its 12-byte header, one
16-bit character per byte value. A byte of 0x80 or more whose entry is below 0x20 starts a two-byte code:
the character is entry[lead entry * 128 + second byte - 0x80] (how Chinese and Japanese fit in bytes).
GW1/GW2 keep both chunks in Win32/loc/<language>.toc; BFN's FsUITextDatabase_<Language> EBX name them.

    python loc.py export --game GAME --dest FOLDER --out result.json   (every string, all languages: CSV + JSON)
"""
import re
import struct
import sys
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


def _decoder(histogram):
    """bytes -> str through a histogram chunk; Latin-1 without one."""
    if not histogram or len(histogram) < 12 + 512:
        return lambda raw: raw.decode('latin-1')
    chars = struct.unpack_from('<%dH' % ((len(histogram) - 12) // 2), histogram, 12)

    def decode(raw):
        out, i = [], 0
        while i < len(raw):
            b = raw[i]
            i += 1
            entry = chars[b]
            if b >= 0x80 and 0 < entry < 0x20 and i < len(raw):
                at = entry * 128 + raw[i] - 0x80
                i += 1
                out.append(chr(chars[at]) if 0 <= at < len(chars) and chars[at] else '�')
            else:
                out.append(chr(entry) if entry else chr(b))
        return ''.join(out)
    return decode


def _table(data, histogram=None):
    _, _, count, table, text = struct.unpack_from('<5I', data, 0)
    pairs = struct.unpack_from('<%dI' % (count * 2), data, table + 8)
    base = text + 8
    decode = _decoder(histogram)
    strings = {}
    for i in range(count):
        at = base + pairs[2 * i + 1]
        end = data.find(b'\0', at)
        if at < len(data) and end > at:
            strings[pairs[2 * i]] = decode(data[at:end])
    return strings


HISTOGRAM = 0x00039001
LANGUAGES = {'br': 'Portuguese (Brazil)', 'de': 'German', 'en': 'English', 'es': 'Spanish', 'fr': 'French',
             'it': 'Italian', 'ja': 'Japanese', 'pl': 'Polish', 'zh': 'Chinese (Traditional)', 'ru': 'Russian'}


def _gw_tables(game):
    """{language code: {hash: text}} from Win32/loc/<language>.toc; the patch's chunks replace the base's."""
    chunks = {}
    for root in gw2.data_roots(game):  # base first, then the patch overrides it
        for toc in sorted((root / 'Win32/loc').glob('*.toc')):
            sb = toc.with_suffix('.sb')
            if not sb.is_file():
                continue
            try:
                for offset, size in _chunks(toc):
                    data = _read(sb, offset, size)
                    if len(data) >= 12:
                        chunks.setdefault(toc.stem.lower(), {})[struct.unpack_from('<I', data)[0]] = data
            except (ValueError, struct.error, OSError):
                continue
    tables = {}
    for language, found in sorted(chunks.items()):
        if MAGIC in found and len(found[MAGIC]) >= 36:
            tables[language] = _table(found[MAGIC], found.get(HISTOGRAM))
    return tables


def _bfn_tables(game):
    """{language: {hash: text}} for Battle for Neighborville: each FsUITextDatabase_<Language> EBX holds its
    two chunk GUIDs, found here by trying every 16 bytes against the archive (no key needed). Only the
    languages installed have their chunks."""
    import explorer
    source = explorer.Source(game)
    assets = source._bfn_assets()
    prefix = 'globals/localization/frostedstrings/textdatabase/fsuitextdatabase_'
    tables = {}
    for name in sorted(n for n in assets['ebx'] if n.startswith(prefix)):
        data = source.ebx_bytes(name) or b''
        found = {}
        for i in range(len(data) - 16):
            guid = data[i:i + 16].hex()
            ref = assets['chunk'].get(guid) or source.archive.chunk(guid)
            if ref:
                chunk = source.archive.fetch(ref)
                if len(chunk) >= 12:
                    found[struct.unpack_from('<I', chunk)[0]] = chunk
        if MAGIC in found:
            tables[name[len(prefix):]] = _table(found[MAGIC], found.get(HISTOGRAM))
    return tables


def tables(game):
    """{language: {hash: text}} of every language the install has; English is 'en' (GW) or 'english' (BFN)."""
    game = Path(game)
    key = ('all', game)
    if key not in _cache:
        from cosmetics import game_kind
        try:
            _cache[key] = _bfn_tables(game) if game_kind(game) == 'bfn' else _gw_tables(game)
        except (ValueError, struct.error, OSError):
            _cache[key] = {}
    return _cache[key]


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
            chunks = {}
            for offset, size in _chunks(toc):
                data = _read(sb, offset, size)
                if len(data) >= 12:
                    chunks[struct.unpack_from('<I', data)[0]] = data
            if MAGIC in chunks and len(chunks[MAGIC]) >= 36:
                found.update(_table(chunks[MAGIC], chunks.get(HISTOGRAM)))
        except (ValueError, struct.error, OSError):
            continue
    _cache[game] = found
    return found


# ---------------- string ID names ----------------
ID_PATTERN = re.compile(rb'ID_[A-Za-z0-9_]{2,}')
_SUFFIXES = ('', '_TITLE', '_DESC', '_DESCRIPTION', '_NAME', '_HINT', '_HEADER', '_SUBTITLE', '_SHORT', '_BODY',
             '_TEXT', '_TOOLTIP', '_LONG', '_INFO', '_LABEL', '_PROMPT', '_SUMMARY', '_FLAVOR', '_PLURAL')


def _grow(found, wanted):
    """Guess more IDs from the ones found: numbered siblings (ID_X_3 -> ID_X_0..ID_X_120) and suffix swaps
    (ID_X_TITLE -> ID_X_DESC). Only guesses whose hash is a real string are kept."""
    bases = set()
    for sid in found:
        core = sid
        for suffix in _SUFFIXES[1:]:
            if core.endswith(suffix):
                core = core[:-len(suffix)]
                break
        bases.add(core)
        stem = re.sub(r'_?\d+$', '', core)
        if stem != core:
            bases.add(stem)
    out = {}
    for base in bases:
        for suffix in _SUFFIXES:
            guess = base + suffix
            value = sid_hash(guess)
            if value in wanted:
                out[value] = guess
        if re.search(r'\d$', base) or base + '_1' in found or base + '_01' in found or any(
                sid_hash(base + s) in wanted for s in ('_1', '_01', '1')):
            for n in range(0, 121):
                for guess in ('%s_%d' % (base, n), '%s_%02d' % (base, n), '%s%d' % (base, n)):
                    value = sid_hash(guess)
                    if value in wanted:
                        out[value] = guess
    return out


def id_names(game, wanted, progress=None):
    """{hash: 'ID_...'} for the string IDs named anywhere in the game's EBX and UI files, cached per install.
    Each top folder is sampled first and skipped when its sample names no strings (meshes, effects, ...)."""
    import hashlib
    import json
    import random
    import explorer
    source = explorer.Source(game)
    kind = source.kind
    if kind == 'bfn':
        names = sorted(source._bfn_assets()['ebx'])
        resources = []
    else:
        names = sorted(source.ebx)
        resources = sorted(n for n, loc in source.resources.items() if loc[3].get('resource_type') in (0x2D47A5FF, 0x5C4954A6))
    key = hashlib.sha256(repr(['ids-v1', str(Path(game).resolve()), len(names), len(wanted), sorted(wanted)[:50]])
                         .encode()).hexdigest()[:16]
    path = explorer.cache_root() / ('string_ids_%s_%s.json' % (kind, key))
    if path.is_file():
        return {int(h): sid for h, sid in json.loads(path.read_text(encoding='utf-8')).items()}
    found = {}

    def scan(data):
        hits = 0
        for match in ID_PATTERN.findall(data or b''):
            sid = match.decode('ascii')
            value = sid_hash(sid)
            if value in wanted:
                found[value] = sid
                hits += 1
        return hits

    folders = {}
    for name in names:
        folders.setdefault(name.split('/')[0], []).append(name)
    todo = []
    rng = random.Random(1)
    for folder, members in sorted(folders.items()):
        sample = set(members if len(members) <= 60 else rng.sample(members, 60))
        if sum(scan(source.ebx_bytes(n)) for n in sample):
            todo += [n for n in members if n not in sample]
    from archive_parallel import ordered_map
    for done, data in enumerate(ordered_map(source.ebx_bytes, todo)):
        scan(data)
        if progress and done % 2000 == 0:
            progress(done, len(todo))
    for name in resources:
        try:
            scan(source.resource(name)[0])
        except (ValueError, struct.error, OSError, TypeError):
            continue
    found.update(_grow(set(found.values()), wanted))
    path.write_text(json.dumps(found), encoding='utf-8')
    return found


# ---------------- export ----------------
def export(game, dest, names=True):
    """All strings in every language to strings.csv (one column per language) and strings.json under dest."""
    import csv
    import json
    found = tables(game)
    if not found:
        raise ValueError('No readable UI text in this install')
    languages = list(found)
    english = next((l for l in languages if l in ('en', 'english')), languages[0])
    languages.remove(english)
    languages.insert(0, english)
    hashes = sorted(set().union(*(set(t) for t in found.values())))
    ids = id_names(game, set(hashes)) if names else {}
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    rows = [{'hash': '%08X' % h, 'id': ids.get(h, ''), 'text': {l: found[l][h] for l in languages if h in found[l]}}
            for h in hashes]
    rows.sort(key=lambda r: (r['id'] == '', r['id'] or r['hash']))
    with (dest / 'strings.csv').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['hash', 'id'] + languages)
        for row in rows:
            writer.writerow([row['hash'], row['id']] + [row['text'].get(l, '') for l in languages])
    (dest / 'strings.json').write_text(json.dumps({'languages': languages, 'names': {l: LANGUAGES.get(l, l.title())
                                                                                      for l in languages},
                                                    'strings': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    return {'written': [str(dest / 'strings.csv'), str(dest / 'strings.json')], 'languages': languages,
            'count': len(rows), 'named': sum(1 for r in rows if r['id']), 'dest': str(dest), 'errors': []}


def browse(game, language=None, names=True):
    """For the app's text list: the languages, and [hash, id, text] of every string in one language."""
    found = tables(game)
    if not found:
        raise ValueError('No readable UI text in this install')
    languages = sorted(found, key=lambda l: (l not in ('en', 'english'), l))
    language = language if language in found else languages[0]
    ids = id_names(game, set().union(*(set(t) for t in found.values()))) if names else {}
    rows = [['%08X' % h, ids.get(h, ''), text] for h, text in found[language].items()]
    rows.sort(key=lambda r: (r[1] == '', r[1] or r[0]))
    return {'languages': [[l, LANGUAGES.get(l, l.title())] for l in languages], 'language': language, 'strings': rows}


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


def main():
    import argparse
    import json
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('export', 'browse'))
    p.add_argument('--game', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--dest')
    p.add_argument('--language')
    a = p.parse_args()
    result = export(a.game, a.dest) if a.action == 'export' else browse(a.game, a.language)
    Path(a.out).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
