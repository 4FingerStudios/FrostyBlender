"""Visual effects (explosions, muzzle flashes, smoke, sparks) from Garden Warfare 1 and 2 and Battle for
Neighborville.

An effect is an EffectBlueprint whose EffectEntityData lists emitters and lights. Each emitter is a
ScalableEmitterDocument: a template (count, lifetime, texture flipbook, mesh) and a chain of
processors that spawn particles (rate, position, direction, speed, size, rotation, colour) and update
them over their life (size, colour, transparency, gravity, drag). Conversion runs the spawn side here,
seeded so the result is repeatable, integrates every particle over its life (gravity, drag, local force,
velocity and spin, each with its over-life curve) and writes the recorded path plus the over-life curves;
the Blender add-on plays them back with Geometry Nodes, so the timeline scrubs without a cache.
GPU emitters are read into the same processors. Semantics follow the game data and V0e's VFX Editor
plugin for Frosty (types, alignments, cone angles, motion stretch).

Battle for Neighborville builds its particles as EmitterGraphs: node graphs compiled into a spawn and a
simulate compute shader. Those shaders are run on the CPU (emitter_graph.py, dxbc.py) and every particle's
position, size, spin, colour and transparency recorded over its life, so they play as the game computes them.

Also simulated: turbulence (a smooth noise field with the processor's intensity, period, octaves, persistence
and over-life curve; the game's own noise differs), world wind (each particle's drift per m/s of wind is
recorded, and the add-on's Wind property sets the level's wind) and collisions with a ground plane at the
effect's origin (bounce with the processor's restitution, come to rest, or die). Graphs that collide with the
depth buffer are given that ground in their depth buffer. Heat-haze (distortion) emitters are kept and
drawn as refraction; lights carry their intensity curve, flicker, loop and spot cone; sounds are exported as
WAV (several layers of a patch mixed into one) for the add-on to put in the Sequencer.

Approximated: alpha erosion, ribbons (flat strips), beams (quads), spline evaluators (straight between points).
Not ported (listed in each effect's notes): collision and spawn sounds, screen flashes (visual environment),
beam end points (location effects), camera shake, and wind inside Battle for Neighborville graphs.

    python effects.py discover --game GAME --out effects.json
    python effects.py export --game GAME --items items.json --out EXPORT [--names BFN_TYPES]
    python effects.py convert --export EXPORT --textures TEXTURES --assets ASSETS --out scene.json

Game files are only read.
"""
import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
from pathlib import Path

import numpy as np

import gw2
from cosmetics import game_kind, title

FOLLOW = {'EffectBlueprint', 'ScalableEmitterDocument', 'EmitterDocument', 'GpuRuntimeEmitterDocument'}
KEEP = FOLLOW | {'TextureAsset', 'AtlasTextureAsset', 'RigidMeshAsset', 'CompositeMeshAsset', 'SkinnedMeshAsset'}
SAMPLES = 17           # points per over-life curve
LOOP_SECONDS = 4.0     # how long a continuous (looping) emitter is played
MAX_PARTICLES = 4000   # per emitter
ATLAS_CACHE = {}
TEXTURE_LIMIT = int(os.environ.get('AURA_IMPORT_TEXTURE_SIZE', '1024'))
GROUP_SKIP = {'fx', '_pvz', 'emitters', 'emitter'}


# ---------------- discovery ----------------
def _effect(name):
    parts = [p for p in name.split('/') if p.lower() not in GROUP_SKIP]
    leaf = re.sub(r'^fx_', '', parts[-1], flags=re.I)
    group = '/'.join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else 'effects')
    return {'name': title(leaf), 'group': group.lower(), 'asset': name, 'bundle': 'asset:' + name, 'sb': ''}


# Battle for Neighborville keeps effects beside what uses them; these folder names say nothing on their own.
BFN_SKIP = GROUP_SKIP | {'characters', 'playable', 'gw2_fx', 'abilities', 'boss', 'subboss', 'horde', 'fx'}
BFN_PARTS = ('_textures', 'textures', '_meshes', 'emitters', '_shader', '_shaders', 'decal', '_ve', 'shaderbox', '_visualguide')


def _bfn_effect(name):
    parts = [p for p in name.split('/') if p.lower() not in BFN_SKIP and not re.fullmatch(r'characters_cu\d+|cu_\d+_\d+', p.lower())]
    leaf = re.sub(r'^fx_', '', parts[-1], flags=re.I)
    group = '/'.join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else 'effects')
    return {'name': title(leaf), 'group': group.lower(), 'asset': name, 'bundle': 'asset:' + name, 'sb': ''}


def bfn_discover(game):
    """Effect blueprints by name (fx_*): bundle manifests list names without the initfs key."""
    import bfn
    archive = bfn.Archive(game)
    names = {}
    for sb in archive.sbs:
        if '/loc/' in sb.lower():
            continue
        for entry in archive.toc(sb)['bundles'].values():
            try:
                listed = archive.bundle(entry)
            except (ValueError, struct.error, IndexError):
                continue
            for a in listed:
                if a['kind'] == 'ebx':
                    names.setdefault(a['name'].lower(), a['name'])
    effects = []
    for low, name in names.items():
        parts = low.split('/')
        leaf = parts[-1]
        if not leaf.startswith('fx_') or leaf.startswith('fx_pf_') or any(p in BFN_PARTS for p in parts[:-1]):
            continue
        if leaf.endswith(('class', '_schematics', '_channel', '_prefab', '_perplayer')):
            continue
        effects.append(_bfn_effect(name))
    return effects


def discover(game):
    kind = game_kind(game)
    if kind == 'bfn':
        effects = bfn_discover(game)
        effects.sort(key=lambda e: (e['group'], e['name']))
        groups = {}
        for e in effects:
            groups.setdefault(e['group'], []).append(e)
        return {'game': kind, 'groups': [{'name': g, 'effects': v} for g, v in sorted(groups.items())]}
    index = gw2.cached_dependency_index(Path(game))
    effects = []
    for location in index['ebx'].values():
        name = location[3]['name']
        low = name.lower()
        if not (low.startswith('fx/') or low.startswith('_pvz/fx/')):
            continue
        if not low.split('/')[-1].startswith('fx_') or '/emitters/' in low or '/_textures/' in low or '/_meshes/' in low:
            continue
        effects.append(_effect(name))
    effects.sort(key=lambda e: (e['group'], e['name']))
    groups = {}
    for e in effects:
        groups.setdefault(e['group'], []).append(e)
    return {'game': kind, 'groups': [{'name': g, 'effects': v} for g, v in sorted(groups.items())]}


# ---------------- export ----------------
BFN_FOLLOW = ('EffectBlueprint', 'EmitterGraph', 'ScalableEmitterDocument', 'GpuRuntimeEmitterDocument',
              'SoundPatchConfigurationAsset')  # a sound configuration names the wave it plays
WAVES = ('SoundWaveAsset', 'NewWaveAsset')


def export(game, items, output, names=None):
    kind = game_kind(game)
    if kind == 'bfn':
        import bfn
        if not names:
            raise ValueError('Battle for Neighborville needs its type name table (--names)')
        inventory = bfn.export(game, 'effects', output, bfn.key_from(None), names,
                               int(os.environ.get('AURA_IMPORT_JOBS', '4')), bundles=[],
                               extra_ebx=[i['asset'] for i in items], follow=BFN_FOLLOW)
        wanted = {i['asset'].lower(): i for i in items}
        for a in inventory['assets']:
            if a.get('name', '').lower() in wanted:
                a['item'] = wanted[a['name'].lower()]['name']
        inventory['items'] = items
        documents = {a['guid']: (a, json.loads((Path(output) / a['document']).read_text(encoding='utf-8')))
                     for a in inventory['assets'] if a.get('document') and a.get('guid')}
        archive = bfn.Archive(Path(game))
        inventory['sounds'] = effect_sounds(lambda guid: documents.get(guid), _bfn_wave(archive), Path(output),
                                            [d for a, d in documents.values() if a.get('root_type') == 'EffectBlueprint'])
        (Path(output) / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
        return inventory
    game, output = Path(game), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'ebx').mkdir(exist_ok=True)
    index = gw2.cached_dependency_index(game)
    by_name = {location[3]['name'].lower(): location for location in index['ebx'].values()}
    inventory = {'profile': kind, 'game': str(game), 'package': 'effects', 'assets': [], 'errors': [],
                 'archive_policy': 'base-only shared index'}
    seen, names, effects = set(), {}, []

    def store(location):
        path, offset, size, source = location
        with path.open('rb') as stream:
            stream.seek(offset)
            content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
        document = gw2.Ebx(content).document()
        if document['guid'] in seen:
            return None
        seen.add(document['guid'])
        names[document['guid']] = source['name']
        if document['root_type'] not in KEEP:
            return None  # sounds and gameplay data are not needed
        asset = dict(source)
        asset['path'] = 'ebx/%s.bin' % source['sha1']
        asset['document'] = 'ebx/%s.json' % source['sha1']
        asset['bytes'] = len(content)
        asset['root_type'], asset['guid'] = document['root_type'], document['guid']
        (output / asset['path']).write_bytes(content)
        (output / asset['document']).write_text(json.dumps(document), encoding='utf-8')
        inventory['assets'].append(asset)
        return asset, document

    queue = []
    for item in items:
        location = by_name.get(item['asset'].lower())
        if location is None:
            inventory['errors'].append({'name': item['asset'], 'stage': 'ebx', 'error': 'Not in the shared index'})
            continue
        found = store(location)
        if found:
            found[0]['item'] = item['name']
            queue.append(found[1])
    # Effects import emitters (and sometimes other effects); emitters import textures and meshes.
    while queue:
        document = queue.pop()
        if document['root_type'] not in FOLLOW:
            continue
        if document['root_type'] == 'EffectBlueprint':
            effects.append(document)
        for ref in document['imports']:
            location = index['ebx'].get(ref['file'])
            if location is None or ref['file'] in seen:
                continue
            found = store(location)
            if found:
                queue.append(found[1])
    inventory['names'] = names  # shaders and sounds are read for their names only
    loaded = {}

    def load(guid):
        if guid not in loaded:
            location = index['ebx'].get(guid)
            loaded[guid] = None
            if location is not None:
                path, offset, size, source = location
                with path.open('rb') as stream:
                    stream.seek(offset)
                    content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
                loaded[guid] = ({'name': source['name']}, gw2.Ebx(content).document())
        return loaded[guid]
    inventory['sounds'] = effect_sounds(load, _gw_wave(game), output, effects)
    gw2.complete_dependencies(game, output, inventory)
    if kind == 'gw1':
        import gw1
        gw1.finish(game, output, inventory)
    else:
        gw2.complete_texture_chunks(game, output, inventory)
    _atlas_chunks(game, kind, output, inventory, index)
    inventory['items'] = items
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=1), encoding='utf-8')
    return inventory


ATLAS_RESOURCE = 0x957C32B1  # AtlasTexture: format, width, height, then the chunk holding its pixels


def _atlas_chunks(game, kind, output, inventory, index):
    """Atlas (flipbook) textures keep their pixels in a chunk that the texture path does not fetch."""
    have = {a['guid'] for a in inventory['assets'] if a['kind'] == 'chunk'}
    xenon = inventory.get('platform') == 'xenon'
    toc = {}
    if kind == 'gw1':
        for base in (root / 'Win32' for root in gw2.data_roots(game)):
            for path in sorted(base.glob('*chunks.toc')):
                for chunk in gw2.read_db(path).get('chunks', []):
                    toc[chunk['id']] = (path.with_suffix('.sb'), chunk)
    for asset in list(inventory['assets']):
        if asset.get('resource_type') != ATLAS_RESOURCE or 'bytes' not in asset:
            continue
        header = (output / asset['path']).read_bytes()
        if len(header) < 32:
            continue
        guid = gw2.guid_big(header[16:32]) if xenon else header[16:32].hex()
        if guid in have and not xenon:
            continue
        content = None
        if guid in index['chunk']:
            path, offset, size, source = index['chunk'][guid]
            with path.open('rb') as stream:
                stream.seek(offset)
                content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
        elif guid in toc:
            import gw1
            path, entry = toc[guid]
            with path.open('rb') as stream:
                stream.seek(entry['offset'])
                data = stream.read(entry['size'])
            content = gw2.decompress(gw2.Reader(data), gw1.stream_size(data))
        if content is None:
            inventory['errors'].append({'name': asset['name'], 'stage': 'atlas', 'error': 'pixel chunk %s not found' % guid})
            continue
        if xenon:
            # Atlas textures use linear BC payloads on Xenon (unlike TextureAsset's tiling).
            content = np.frombuffer(content, np.uint8).reshape(-1, 2)[:, ::-1].copy().tobytes()
            normalized = bytearray(header)
            struct.pack_into('<HH', normalized, 2, *struct.unpack_from('>HH', header, 2))
            normalized[16:32] = bytes.fromhex(guid)
            asset['path'] = asset['path'][:-4] + '.atlas.bin'
            (output / asset['path']).write_bytes(normalized)
        rel = 'chunk/%s.bin' % hashlib.sha1(content).hexdigest()
        (output / rel).parent.mkdir(parents=True, exist_ok=True)
        (output / rel).write_bytes(content)
        inventory['assets'].append({'kind': 'chunk', 'guid': guid, 'name': guid, 'path': rel, 'bytes': len(content),
                                    'original_size': len(content)})
        have.add(guid)


# ---------------- sounds ----------------
RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)


def _sps_start(data, limit=4096):
    """Offset of the first EA sound stream header in `data`, or None. Battle for Neighborville's wave chunks
    put a small table of their own before it."""
    import audio
    for at in range(0, min(len(data), limit)):
        if data[at] != 0x48:
            continue
        try:
            info = audio.header(data, at)
        except (ValueError, struct.error):
            continue
        if info['rate'] in RATES and info['codec'] in audio.CODECS and info['samples']:
            return at
    return None


def _gw_wave(game):
    """wave(name, root, folder) -> (WAV path, variation count) for a Garden Warfare SoundWaveAsset."""
    import audio
    chunks = []

    def wave(name, root, folder):
        if not chunks:
            chunks.append(audio.Chunks(game))  # every chunk TOC of the install, read once
        written = audio.export_sound(None, chunks[0], name, root, folder)
        return written[0], len(written)
    return wave


def _bfn_wave(archive):
    """wave(name, root, folder) for a Battle for Neighborville NewWaveAsset: its first stream, decoded by audio.py."""
    import audio

    def wave(name, root, folder):
        listed = [c.get('ChunkId') for c in root.get('Chunks') or [] if c.get('ChunkId')]
        if not listed:
            raise ValueError('the wave has no sound data chunk')
        ref = archive.chunk(listed[0])
        if not ref:
            raise ValueError('its sound data chunk is not in the archives')
        data = archive.fetch(ref)
        at = _sps_start(data)
        if at is None:
            raise ValueError('no EA sound stream in its chunk')
        target = Path(folder) / (name.replace('\\', '/') + '.wav')
        try:
            rate, pcm = audio.decode_stream(data, at)
            audio.write_wav(target, rate, pcm)
        except NotImplementedError:
            audio.via_vgmstream(data, at, target)  # codecs only vgmstream reads, when one is installed
        return str(target), 1
    return wave


def _sound(load, wave, output, guid):
    found = load(guid)
    if not found:
        raise ValueError('sound asset not exported')
    asset, doc = found
    root = doc['objects'][0]
    info = {'name': asset['name'], 'loop': bool(root.get('IsLooping')), 'loudness': float(root.get('Loudness', 0) or 0)}
    # A configuration (BFN) plays a shared patch; the patch says whether it loops.
    patch = root.get('Sound') or {}
    if patch.get('$pointer') == 2:
        parent = load(doc['imports'][patch['index']]['file'])
        if parent:
            info['loop'] = info['loop'] or bool(parent[1]['objects'][0].get('IsLooping')) or 'loop' in parent[0]['name'].lower()
    # A patch or configuration lists the waves it plays as referenced data.
    waves = [found] if doc['root_type'] in WAVES else []
    for reference in root.get('ReferencedData') or []:
        data = reference.get('SoundData') or {}
        if data.get('$pointer') == 2:
            w = load(doc['imports'][data['index']]['file'])
            if w and w[1]['root_type'] in WAVES:
                waves.append(w)
    if not waves:
        raise ValueError('no sound wave in %s' % asset['name'].split('/')[-1])
    # A patch plays its waves together (an explosion's body, low end and debris): they are mixed into one.
    written, count, failed = [], 0, None
    for w in waves[:MAX_LAYERS]:
        try:
            path, variations = wave(w[0]['name'], w[1]['objects'][0], Path(output) / 'sounds')
        except Exception as error:  # a layer that does not decode leaves the others
            failed = failed or error
            continue
        written.append(path)
        count = max(count, variations)
    if not written:
        raise failed
    path = written[0] if len(written) == 1 else _mix(written, Path(output) / 'sounds' / (asset['name'] + '.mix.wav'))
    info.update(wav=Path(path).relative_to(output).as_posix(), variations=count, waves=len(waves))
    return info


MAX_LAYERS = 8


def _mix(paths, target):
    """The WAV files at `paths` played together, at the highest of their rates, as one WAV (peak kept below 1)."""
    import wave as wavefile
    import audio
    layers = []
    for path in paths:
        with wavefile.open(str(path), 'rb') as f:
            pcm = np.frombuffer(f.readframes(f.getnframes()), '<i2').reshape(-1, f.getnchannels()).astype(np.float64)
            layers.append((f.getframerate(), pcm))
    rate = max(r for r, _ in layers)
    channels = max(p.shape[1] for _, p in layers)
    length = max(int(len(p) * rate / r) for r, p in layers)
    out = np.zeros((length, channels))
    for r, pcm in layers:
        n = int(len(pcm) * rate / r)
        at = np.arange(n) * r / rate
        for c in range(channels):
            out[:n, c] += np.interp(at, np.arange(len(pcm)), pcm[:, min(c, pcm.shape[1] - 1)])
    peak = np.abs(out).max(initial=0)
    if peak > 32000:
        out *= 32000 / peak
    audio.write_wav(Path(target), rate, out.astype(np.int16))
    return str(target)


def effect_sounds(load, wave, output, effects):
    """{sound file GUID: {'name', 'wav' (relative to `output`), 'loop', 'loudness', ...} or {'error'}} for every
    SoundEffectEntityData of the effect documents. load(guid) -> (asset, document) or None."""
    out = {}
    for doc in effects:
        for o in doc['objects']:
            ref = (o.get('Sound') or {}) if o.get('$type') == 'SoundEffectEntityData' else {}
            if ref.get('$pointer') != 2:
                continue
            guid = doc['imports'][ref['index']]['file']
            if guid in out:
                continue
            try:
                out[guid] = _sound(load, wave, output, guid)
            except Exception as error:  # one sound must not stop the export
                out[guid] = {'error': str(error)}
    return out


def _bc_colors(blocks):
    """BC1 colour halves (n, 8 bytes) -> (n, 16, 3) uint8."""
    c = blocks[:, :4].copy().view('<u2').astype(np.int32)
    rgb = np.stack([((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31], -1)  # (n, 2, 3)
    c0, c1 = rgb[:, 0], rgb[:, 1]
    four = (c[:, 0] > c[:, 1])[:, None]
    p2 = np.where(four, (2 * c0 + c1) // 3, (c0 + c1) // 2)
    p3 = np.where(four, (c0 + 2 * c1) // 3, 0)
    palette = np.stack([c0, c1, p2, p3], 1)  # (n, 4, 3)
    bits = blocks[:, 4:8].copy().view('<u4')[:, 0]
    idx = (bits[:, None] >> (2 * np.arange(16))) & 3
    return np.take_along_axis(palette, idx[:, :, None].astype(np.int64), 1).astype(np.uint8)


def _bc3_alpha(blocks):
    a = blocks[:, :2].astype(np.int32)
    a0, a1 = a[:, 0:1], a[:, 1:2]
    i = np.arange(1, 7)
    eight = a0 > a1
    seven = ((7 - i) * a0 + i * a1) // 7               # (n, 6)
    five = ((5 - i[:4]) * a0 + i[:4] * a1) // 5          # (n, 4)
    five = np.concatenate([five, np.zeros((len(a), 1), np.int32), np.full((len(a), 1), 255)], 1)
    palette = np.concatenate([a0, a1, np.where(eight, seven, five)], 1)  # (n, 8)
    bits = np.zeros(len(a), np.uint64)
    for k in range(6):
        bits |= blocks[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
    idx = (bits[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)
    return np.take_along_axis(palette, idx.astype(np.int64), 1).astype(np.uint8)


def atlas_pixels(header, data, limit=1024):
    """RGBA rows of an atlas texture's top mip (BC1 or BC3), halved until it fits `limit`."""
    width, height = struct.unpack_from('<HH', header, 2)
    if not width or not height:
        raise ValueError('atlas has no size')
    bw, bh = max(width // 4, 1), max(height // 4, 1)
    total = len(data) * 3 / 4  # the chain of mips is a third larger than the top one
    block = 16 if total >= bw * bh * 16 * 0.95 else 8
    raw = np.frombuffer(data[:bw * bh * block], np.uint8).reshape(-1, block)
    if block == 16:
        rgb, alpha = _bc_colors(raw[:, 8:]), _bc3_alpha(raw[:, :8])
    else:
        rgb, alpha = _bc_colors(raw), np.full((len(raw), 16), 255, np.uint8)
    rgba = np.concatenate([rgb, alpha[:, :, None]], 2).reshape(bh, bw, 4, 4, 4)
    image = rgba.transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    while image.shape[0] > limit or image.shape[1] > limit:
        image = image.reshape(image.shape[0] // 2, 2, image.shape[1] // 2, 2, 4).mean((1, 3)).astype(np.uint8)
    return image


def atlas_texture(archive, assets, guid, cache, limit):
    """PNG (relative to `assets`) for an AtlasTextureAsset, decoded here because the reader skips atlases."""
    if guid in cache:
        return cache[guid]
    from convert import png
    cache[guid] = None
    found = archive.by_guid.get(guid)
    if not found:
        return None
    rid = found[1]['objects'][0].get('Resource', {}).get('$resource')
    resource = archive.resources.get(rid)
    if not resource:
        return None
    header = (archive.root / resource['path']).read_bytes()
    chunk = archive.chunks.get(header[16:32].hex())
    if not chunk:
        return None
    image = atlas_pixels(header, (archive.root / chunk['path']).read_bytes(), limit)
    rel = 'textures/frostbite/%s.png' % guid
    png(Path(assets) / rel, image.shape[1], image.shape[0], image.tobytes())
    cache[guid] = rel
    return rel


# ---------------- emitter evaluation ----------------
def _vec(value, keys='xyz', default=0.0):
    value = value or {}
    return np.array([float(value.get(k, default)) for k in keys])


def _poly(coefficients, t):
    """Frostbite cubics: x t^3 + y t^2 + z t + w."""
    x, y, z, w = _vec(coefficients, 'xyzw')
    return ((x * t + y) * t + z) * t + w


def _unit(n, rng):
    v = rng.normal(size=(n, 3))
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


class Emitter:
    """Evaluates one emitter template's processor chain."""

    def __init__(self, archive, doc, template, seed, notes, processors=None):
        self.archive, self.doc, self.template, self.notes = archive, doc, template, notes
        self.rng = np.random.default_rng(seed)
        self.processors = list(processors or [])
        if processors is not None:
            return
        node = self.obj(template.get('RootProcessor'))
        guard = 0
        while node is not None and guard < 200:
            self.processors.append(node)
            node = self.obj(node.get('NextProcessor'))
            guard += 1

    def obj(self, pointer):
        found = self.archive.resolve(self.doc, pointer)
        return found[1] if found else None

    def find(self, kind):
        return next((p for p in self.processors if p['$type'] == kind), None)

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    # An evaluator turns an input (0..1) into a value per particle: a scalar, a vector or a colour.
    def evaluate(self, pointer, t):
        o = self.obj(pointer)
        if o is None:
            return None
        n = len(t)
        kind = o['$type']
        if kind == 'PolynomialData':
            v = _poly(o.get('Coefficients'), t) * float(o.get('ScaleValue', 1.0))
            return np.clip(v, float(o.get('MinClamp', -1e9)), float(o.get('MaxClamp', 1e9)))
        if kind == 'RandomEvaluatorData':
            lo, hi = float(o.get('Min', 0)), float(o.get('Max', 1))
            return lo + self.rng.random(n) * (hi - lo)
        if kind == 'DefaultEvaluatorData':
            return np.tile(_vec(o.get('Values'), 'xyzw'), (n, 1))
        if kind in ('RandomXYZEvaluatorData', 'RandomXYZWEvaluatorData'):
            lo = np.array([float(o.get('Min' + c, 0)) for c in 'XYZ'])
            hi = np.array([float(o.get('Max' + c, 0)) for c in 'XYZ'])
            return lo + self.rng.random((n, 3)) * (hi - lo)
        if kind == 'SphereEvaluatorData':
            r = float(o.get('Radius', 1)) * np.cbrt(self.rng.random(n))[:, None]
            return _unit(n, self.rng) * r * _vec(o.get('Scale'), default=1) + _vec(o.get('Pivot'))
        if kind == 'SuperSphereEvaluatorData':
            inner, outer = float(o.get('InnerRadius', 0)), float(o.get('OuterRadius', 1))
            r = inner + (outer - inner) * self.rng.random(n)
            zenith = np.radians(o.get('StartZenithAngle', 0) + (o.get('EndZenithAngle', 180) - o.get('StartZenithAngle', 0)) * self.rng.random(n))
            azimuth = np.radians(o.get('StartAzimuthAngle', 0) + (o.get('EndAzimuthAngle', 360) - o.get('StartAzimuthAngle', 0)) * self.rng.random(n))
            v = np.stack([np.sin(zenith) * np.cos(azimuth), np.cos(zenith), np.sin(zenith) * np.sin(azimuth)], 1)
            return v * r[:, None] * _vec(o.get('Scale'), default=1) + _vec(o.get('Pivot'))
        if kind == 'BoxEvaluatorData':
            return (self.rng.random((n, 3)) - 0.5) * _vec(o.get('Dimensions')) + _vec(o.get('Pivot'))
        if kind == 'PolynomialColorInterpData':
            f = np.clip(_poly(o.get('Coefficients'), t), 0, 1)[:, None]
            return _vec(o.get('Color0')) * (1 - f) + _vec(o.get('Color1')) * f
        if kind == 'RotateVectorData':
            # A cone `Angle` degrees from its input direction (up when it has none).
            axis = self.evaluate(o.get('Pre'), t) if o.get('Pre') else None
            axis = axis[:, :3] if axis is not None and axis.ndim == 2 else np.tile([0.0, 1.0, 0.0], (n, 1))
            axis = axis / np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-9)
            half = math.radians(min(float(o.get('Angle', 0)), 180.0))
            cos_t = 1 - self.rng.random(n) * (1 - math.cos(half))
            sin_t = np.sqrt(np.maximum(1 - cos_t ** 2, 0))
            phi = self.rng.random(n) * 2 * math.pi
            helper = np.where(np.abs(axis[:, 1:2]) > 0.99, [[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]])
            u = np.cross(axis, helper)
            u /= np.maximum(np.linalg.norm(u, axis=1, keepdims=True), 1e-9)
            w = np.cross(axis, u)
            return axis * cos_t[:, None] + (u * np.cos(phi)[:, None] + w * np.sin(phi)[:, None]) * sin_t[:, None]
        if kind == 'PolynomialOperatorData':
            # Two clamped cubics combined (a ramp up times a ramp down makes a swell), then clamped.
            a, b = (np.clip(_poly(p.get('Coefficients'), t) * float(p.get('ScaleValue', 1.0)),
                            float(p.get('MinClamp', -1e9)), float(p.get('MaxClamp', 1e9)))
                    for p in (o.get('FirstOperand') or {}, o.get('SecondOperand') or {}))
            op = {0: np.multiply, 1: np.add, 2: np.subtract, 3: np.minimum, 4: np.maximum}.get(o.get('Operation', 0), np.multiply)
            return np.clip(op(a, b), float(o.get('MinClampResult', -1e9)), float(o.get('MaxClampResult', 1e9)))
        if kind == 'SplineData':
            # Up to 12 points: X in XValues0..2 (rising, then zero padding), Y in YValues0..2; straight between them.
            curve = o.get('SplineCurve') or {}
            xs = [v for k in ('XValues0', 'XValues1', 'XValues2') for v in _vec(curve.get(k), 'xyzw')]
            ys = [v for k in ('YValues0', 'YValues1', 'YValues2') for v in _vec(curve.get(k), 'xyzw')]
            count = next((i for i in range(1, len(xs)) if xs[i] <= xs[i - 1]), len(xs))
            return np.interp(t, xs[:count], ys[:count])
        if o.get('Pre'):
            self.note('%s approximated by its input' % kind)
            return self.evaluate(o.get('Pre'), t)
        self.note('%s ignored' % kind)
        return None

    def scalar(self, processor, n, emitter_t=None):
        """A spawn processor's evaluator as one number per particle (1 when it has none)."""
        if processor is None:
            return np.ones(n)
        t = emitter_t if processor.get('EvaluatorInput') == 7 and emitter_t is not None else self.rng.random(n)
        v = self.evaluate(processor.get('Pre'), t)
        if v is None:
            return np.ones(n)
        return v[:, 0] if v.ndim == 2 else v

    def vector(self, processor, n, default):
        v = self.evaluate(processor.get('Pre'), self.rng.random(n)) if processor else None
        if v is None or v.ndim != 2:
            return np.tile(np.asarray(default, float), (n, 1))
        return v[:, :3]

    def curve(self, kind, default=1.0):
        """An update processor's evaluator sampled over the particle's life (0..1)."""
        p = self.find(kind)
        t = np.linspace(0, 1, SAMPLES)
        if p is None:
            return None
        v = self.evaluate(p.get('Pre'), t) if p.get('EvaluatorInput') == 2 else None
        if v is None:
            return [default] * SAMPLES if kind != 'UpdateColorData' else None
        if v.ndim == 2 and kind != 'UpdateColorData':
            v = v[:, 0]
        return v.tolist()

    # ---- spawning
    def spawn_times(self, duration, loop):
        # Ribbons spawn by distance travelled; a still emitter is played at the same rate per second.
        rate = self.find('SpawnRateData') or self.find('SpawnRibbonRateData')
        if rate is None:
            burst = int(self.template.get('MaxCount', 1))
            return np.zeros(min(burst, MAX_PARTICLES))
        base = float(rate.get('SpawnRate', 0))
        if base <= 0:
            return np.zeros(0)
        dt = 1.0 / 240
        steps = np.arange(0, duration, dt)
        if rate.get('EvaluatorInput') == 7 and rate.get('Pre'):
            scale = self.evaluate(rate.get('Pre'), steps / max(duration, 1e-6))
            scale = scale[:, 0] if scale is not None and scale.ndim == 2 else scale
        else:
            scale = None
        r = base * (scale if scale is not None else 1.0) * np.ones(len(steps))
        cumulative = np.cumsum(r * dt)
        count = int(cumulative[-1]) if len(cumulative) else 0
        if count == 0 and not loop:
            count = 1
        times = np.interp(np.arange(count) + 0.5, cumulative, steps) if count else np.zeros(0)
        return times[:MAX_PARTICLES]

    def lifetimes(self, n):
        age = self.find('UpdateAgeData')
        if age is None:
            return np.full(n, 1.0)
        life = float(age.get('Lifetime', 1)) * self.scalar(age, n)
        spread = float(age.get('RandomLifetimeScale', 0))
        return np.maximum(life * (1 - spread * self.rng.random(n)), 1e-3)

    def limit(self, spawn, life):
        """Respect MaxCount: a particle is not spawned while that many are alive."""
        cap = int(self.template.get('MaxCount', 0) or 0)
        if cap <= 0 or len(spawn) <= cap:
            return np.ones(len(spawn), bool)
        keep = np.zeros(len(spawn), bool)
        deaths = []
        for i, t in enumerate(spawn):
            deaths = [d for d in deaths if d > t]
            if len(deaths) < cap:
                keep[i] = True
                deaths.append(t + life[i])
        return keep


def _loops(template):
    return float(template.get('Lifetime', 0) or 0) <= 0


# EmittableType and EmittableAlignment values, matched against V0e's VFX Editor plugin and the game data.
EMITTABLE = {0: 'point', 100: 'quad', 200: 'mesh', 300: 'ribbon', 400: 'beam'}
ALIGN = {0: 'camera', 1: 'camera', 2: 'world', 3: 'velocity', 4: 'camera', 5: 'camera', 6: 'stretch', 7: 'stretch'}
KEYS = 17      # recorded points along each particle's path
STEPS = 64     # integration steps per particle life


def life_curve(e, processor, n, emitter_t):
    """f(life 0..1) -> per-particle multiplier from an update processor's evaluator (1 when it has none).
    Curves of the particle's life are evaluated as it ages; any other input is fixed at spawn."""
    if processor is None or not processor.get('Pre'):
        return lambda f: np.ones(n)
    if processor.get('EvaluatorInput') == 2:
        probe = e.evaluate(processor.get('Pre'), np.zeros(2))
        if probe is None:
            return lambda f: np.ones(n)

        def at(f):
            v = e.evaluate(processor.get('Pre'), np.full(n, f))
            v = v[:, 0] if v.ndim == 2 else v
            return v
        return at
    fixed = e.scalar(processor, n, emitter_t)
    return lambda f: fixed


def _lattice(cells, seed):
    """A repeatable pseudo-random value in -1..1 at each integer lattice point (cells: (..., 3) int64)."""
    h = (cells[..., 0] * 73856093) ^ (cells[..., 1] * 19349663) ^ (cells[..., 2] * 83492791) ^ (seed * 2654435761)
    h = (h ^ (h >> 13)) * 1274126177
    return ((h ^ (h >> 16)) & 0xFFFF).astype(np.float64) / 32767.5 - 1.0


def _noise(q, seed):
    """Smooth value noise (trilinear between lattice values, smoothstepped) at points q (n, 3)."""
    base = np.floor(q)
    f = q - base
    f = f * f * (3 - 2 * f)
    base = base.astype(np.int64)
    out = np.zeros(len(q))
    for corner in range(8):
        offset = np.array([corner & 1, corner >> 1 & 1, corner >> 2 & 1])
        weight = np.prod(np.where(offset, f, 1 - f), axis=1)
        out += weight * _lattice(base + offset, seed)
    return out


def turbulence_force(q, octaves, persistence):
    """A smooth 3D force field (one noise per axis) summed over octaves, about -1..1 per axis."""
    total, amplitude, scale, norm = np.zeros((len(q), 3)), 1.0, 1.0, 0.0
    for octave in range(max(int(octaves), 1)):
        for axis in range(3):
            total[:, axis] += amplitude * _noise(q * scale, 17 * axis + 101 * octave + 1)
        norm += amplitude
        amplitude *= persistence
        scale *= 2.0
    return total / max(norm, 1e-6) * 1.6  # value noise rarely reaches 1; bring it back to about -1..1


def _randomness(e, processor, n):
    return 1 - float(processor.get('PerParticleRandomness', 0) or 0) * e.rng.random(n) if processor else np.ones(n)


GROUND = 0.0  # height of the ground plane in the effect's own space (the effect sits on the ground)


def simulate(e, position, velocity, life, emitter_t, notes, transform=None, size=None, extras=None):
    """Euler-integrate every particle over its own life (as the game and the VFX Editor do), recording KEYS
    points of its path and its spin angle, so forces with over-life curves come out right.
    With `extras` (a dict), what the add-on needs beyond the path is put there: 'wind' (how far one m/s of
    world wind has carried each particle by each key) and 'end' (the part of its life after which a
    collision has killed it; its path past that is not drawn)."""
    n = len(life)
    g = e.find('GravityData')
    gravity = np.array([0.0, -float(g.get('Gravity', 0)), 0.0]) if g else np.zeros(3)
    g_scale = 1 - float(g.get('PerParticleRandomness', 0) or 0) * e.rng.random(n) if g else np.ones(n)
    g_curve = life_curve(e, g, n, emitter_t)
    forces = [p for p in e.processors if p['$type'] == 'LocalForceData']
    local = sum((_vec(p.get('LocalForce')) for p in forces), np.zeros(3))
    l_scale = 1 - float(forces[0].get('PerParticleRandomness', 0) or 0) * e.rng.random(n) if forces else np.ones(n)
    l_curve = life_curve(e, forces[0], n, emitter_t) if forces else (lambda f: np.ones(n))
    a = e.find('AirResistanceData')
    drag = max(float(a.get('DragFactor', 0)), 0.0) if a else 0.0
    d_curve = life_curve(e, a, n, emitter_t)
    moves = e.find('UpdateLinearVelocityData')
    v_curve = life_curve(e, moves, n, emitter_t)
    rot_p, spin_p = e.find('SpawnRotationData'), e.find('SpawnRotationSpeedData')
    angle = float(rot_p.get('Rotation', 0)) * e.scalar(rot_p, n) * 2 * math.pi if rot_p else np.zeros(n)
    spin = float(spin_p.get('RotationSpeed', 0)) * e.scalar(spin_p, n) * 2 * math.pi if spin_p else np.zeros(n)
    r = e.find('UpdateRotationData')
    r_curve = life_curve(e, r, n, emitter_t)
    if spin_p and r is None:
        spin = np.zeros(n)
    # Turbulence: a smooth noise force field (its octaves and persistence), as a force or, for the part
    # TurbulenceForceAsInstantVelocity says, as a velocity added for that step only.
    t = e.find('TurbulanceData')
    turbulence = float(t.get('Intensity', 0) or 0) * _vec(t.get('Multiplier'), default=1) if t else np.zeros(3)
    period = max(float(t.get('PeriodSpace', 1) or 1), 1e-3) if t else 1.0
    octaves = int(t.get('Octaves', 1) or 1) if t else 1
    persistence = float(t.get('OctavePersistence', 0.5) or 0.5) if t else 0.5
    instant = min(max(float(t.get('TurbulenceForceAsInstantVelocity', 0) or 0), 0.0), 1.0) if t else 0.0
    t_scale, t_curve = _randomness(e, t, n), life_curve(e, t, n, emitter_t)
    phase = e.rng.random(3) * 100  # one field per emitter, so neighbouring particles swirl together
    # World wind: the particles drift with WindMultiplier of the wind's speed. The wind's direction and speed
    # belong to the level, so each particle's drift per m/s of wind is recorded and the add-on scales it.
    w = e.find('WorldWindData')
    wind = float(w.get('WindMultiplier', 0) or 0) if w else 0.0
    w_scale, w_curve = _randomness(e, w, n), life_curve(e, w, n, emitter_t)
    # Collisions with the ground plane: bounce (Restitution), come to rest under RestSpeedThreshold, or die.
    c = e.find('UpdateCollisionData')
    collide = c is not None or (e.template.get('$gpu_collision') or 0) > 0
    if c is not None:
        restitution = float(c.get('Restitution', 0.5) or 0)
        rest_speed = float(c.get('RestSpeedThreshold', 0) or 0)
        scatter = float(c.get('Randomness', 0) or 0)
        radius_factor = float(c.get('CollisionRadiusFactor', 1) or 0)
        kill = bool(c.get('KillOnCollision'))
        if c.get('Sound') and c['Sound'].get('$pointer'):
            notes.append('collision sounds not played')
    else:  # GPU emitters collide with the depth buffer: CollisionThickness deep, losing CollisionDampening
        restitution = 1 - min(max(float(e.template.get('$gpu_damping', 0) or 0), 0.0), 1.0)
        rest_speed, scatter, radius_factor, kill = 0.05, 0.0, 0.0, False
    if moves is None:  # nothing moves the particles
        velocity = np.zeros_like(velocity)
        gravity, local, turbulence, wind = np.zeros(3), np.zeros(3), np.zeros(3), 0.0

    p, v = position.astype(float).copy(), velocity.astype(float).copy()
    if collide:
        # The ground in emitter space: height = up . p + offset, from the emitter's place in the effect.
        m = np.asarray(transform if transform is not None else np.eye(4), float)
        scale = max(np.linalg.norm(m[1, :3]), 1e-9)
        up, offset = m[1, :3] / scale, m[1, 3] / scale
        floor = min(GROUND, float((p @ up).min(initial=0.0) + offset)) - offset  # never below a spawn point
        radius = (np.asarray(size, float) if size is not None else np.ones(n)) * 0.5 * radius_factor
        resting = np.zeros(n, bool)
        end = np.ones(n)
        hits = 0
    dt = (life / STEPS)[:, None]
    path, spins, drift = [p.copy()], [angle.copy()], [np.zeros(n)]
    carried = np.zeros(n)
    for s in range(STEPS):
        f = s / STEPS
        acc = gravity * (g_scale * g_curve(f))[:, None] + local * (l_scale * l_curve(f))[:, None]
        kick = 0.0
        if turbulence.any():
            force = turbulence * turbulence_force(p / period + phase, octaves, persistence) * (t_scale * t_curve(f))[:, None]
            acc = acc + force * (1 - instant)
            kick = force * instant
        v = v + acc * dt
        if drag:
            v = v * np.exp(-drag * np.maximum(d_curve(f), 0)[:, None] * dt)
        step = (v + kick) * v_curve(f)[:, None] * dt
        if collide:
            step[resting] = 0.0
            v[resting] = 0.0
        p = p + step
        if wind:
            carried = carried + wind * w_scale * w_curve(f) * dt[:, 0]
        if collide:
            height = p @ up - floor - radius
            hit = (height < 0) & ~resting & (end >= 1)
            if hit.any():
                hits += int(hit.sum())
                if kill:
                    end[hit] = (s + 1) / STEPS
                else:
                    p[hit] -= up * height[hit, None]
                    vn = v[hit] @ up
                    bounce = v[hit] - up * ((1 + restitution) * np.minimum(vn, 0))[:, None]
                    if scatter:
                        bounce += _unit(int(hit.sum()), e.rng) * np.linalg.norm(bounce, axis=1, keepdims=True) * scatter
                        bounce -= up * np.minimum(bounce @ up, 0)[:, None]  # still away from the ground
                    normal = up * (bounce @ up)[:, None]
                    bounce = normal + (bounce - normal) * restitution ** 0.5  # some grip along the ground
                    v[hit] = bounce
                    settle = max(rest_speed, math.sqrt(2 * float(np.linalg.norm(gravity)) * 0.005))
                    resting[np.flatnonzero(hit)[np.linalg.norm(bounce, axis=1) < settle]] = True
        angle = angle + spin * r_curve(f) * dt[:, 0] * (~resting if collide else 1)
        path.append(p.copy())
        spins.append(angle.copy())
        drift.append(carried.copy())
    path, spins, drift = np.stack(path, 1), np.stack(spins, 1), np.stack(drift, 1)
    every = STEPS // (KEYS - 1)
    track, angles, drift = path[:, ::every], spins[:, ::every], drift[:, ::every]
    if extras is not None:
        if wind:
            extras['wind'] = drift
            notes.append('world wind x%g (set the wind on the effect)' % wind)
        if collide:
            extras['end'] = end
            notes.append('%s the ground: %d hits' % ('dies on' if kill else 'bounces off', hits) if hits else
                         'collides with the ground (no particle reached it)')
    return track, angles


def _direction(e, position, n, up):
    """Spawn directions: the direction processor's evaluator, bent toward the emitter's origin as
    DirectionFromEmitterOrigin asks."""
    p = e.find('SpawnDirectionData')
    d = e.vector(p, n, up)
    d = d / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    k = float(p.get('DirectionFromEmitterOrigin', 0) or 0) if p else 0.0
    if k:
        out = position / np.maximum(np.linalg.norm(position, axis=1, keepdims=True), 1e-9)
        out[np.linalg.norm(position, axis=1) < 1e-4] = up
        d = d * (1 - k) + out * k
        d = d / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    return d


def emitter_data(archive, converter, doc, template, name, transform, delay, seed, processors=None, gpu=False,
                 distortion=None):
    """One emitter as effect JSON. `distortion` (a strength) draws it as heat haze that bends what is behind it."""
    notes = []
    e = Emitter(archive, doc, template, seed, notes, processors)
    loop = _loops(template)
    duration = LOOP_SECONDS if loop else float(template['Lifetime'])
    spawn = e.spawn_times(duration, loop)
    n = len(spawn)
    emitter_t = spawn / max(duration, 1e-6)
    life = e.lifetimes(n)
    keep = e.limit(spawn, life)
    up = np.array([0.0, 1.0, 0.0])

    position = e.vector(e.find('SpawnPositionData'), n, [0, 0, 0])
    direction = _direction(e, position, n, up)
    speed_p = e.find('SpawnSpeedData')
    speed = float(speed_p.get('Speed', 0)) * e.scalar(speed_p, n, emitter_t) if speed_p else np.zeros(n)
    size_p = e.find('SpawnSizeData')
    size = float(size_p.get('Size', 1)) * e.scalar(size_p, n, emitter_t) if size_p else np.ones(n)
    frame_p = e.find('SpawnAnimationFrameData')
    frame = np.floor(float(frame_p.get('AnimationFrame', 0)) * e.scalar(frame_p, n)) if frame_p else np.zeros(n)
    tint_p = e.find('SpawnColorRandomData')
    if tint_p:
        f = e.scalar(tint_p, n)[:, None]
        tint = _vec(tint_p.get('Color0'), default=1) * (1 - f) + _vec(tint_p.get('Color1'), default=1) * f
    else:
        tint = np.ones((n, 3))
    flip = np.stack([e.rng.random(n) < float(template.get('FlipUProbability', 0) or 0),
                     e.rng.random(n) < float(template.get('FlipVProbability', 0) or 0)], 1).astype(float)
    extras = {}
    track, angles = simulate(e, position, direction * speed[:, None], life, emitter_t, notes,
                             transform_matrix(transform), size, extras)

    color = e.curve('UpdateColorData')
    base_color = _vec((e.find('UpdateColorData') or {}).get('Color'), default=1)
    color = [base_color.tolist()] * SAMPLES if color is None else (np.asarray(color) * base_color).tolist()
    texture_coords = e.find('UpdateTextureCoordsData')
    texture, atlas = None, {}
    if texture_coords:
        found = archive.resolve(doc, texture_coords.get('BaseTexture'))
        if found:
            if found[1].get('$type') == 'AtlasTextureAsset':
                atlas = found[1]
                texture = atlas_texture(archive, converter.assets, found[0]['guid'], ATLAS_CACHE, TEXTURE_LIMIT)
            else:
                texture = converter.texture(found[0]['guid'])
    anim = e.find('SpawnAnimationData')
    mesh = None
    found = archive.resolve(doc, template.get('Mesh'))
    if found and found[0].get('root_type') in ('RigidMeshAsset', 'CompositeMeshAsset'):
        try:
            mesh = converter.convert_static(found, 0)
        except Exception as error:  # a mesh that does not convert falls back to quads
            notes.append('mesh particle not converted: %s' % error)
    kind = EMITTABLE.get(template.get('EmittableType'), 'quad')
    if mesh:
        kind = 'mesh'
    elif kind == 'mesh':
        kind = 'quad'
    if kind == 'beam':
        notes.append('beam drawn as quads')
    align = ALIGN.get(template.get('EmittableAlignment', 0), 'camera')
    stretch = float(template.get('MotionStretchMultiplier', 1) or 0)
    if gpu and float(template.get('StreakFactor', 0) or 0) > 0:
        align, stretch = 'stretch', float(template['StreakFactor'])
    alpha = e.curve('UpdateTransparencyData')
    level = e.find('UpdateAlphaLevelMinData'), e.find('UpdateAlphaLevelMaxData')
    if alpha and any(level):
        lo = float(level[0].get('MinLevel', 0)) if level[0] else 0.0
        hi = float(level[1].get('MaxLevel', 1)) if level[1] else 1.0
        alpha = [min(max(a, lo), hi) for a in alpha]

    alive = keep
    out = {
        'name': name, 'kind': kind, 'transform': transform_matrix(transform), 'delay': delay, 'loop': loop,
        'duration': duration, 'alignment': template.get('EmittableAlignment', 0), 'align': align,
        'stretch': stretch, 'stretch_clamp': float(template.get('MotionStretchLengthClamp', 100) or 100),
        'stretch_relative_clamp': float(template.get('MotionStretchRelativeLengthClamp', 100) or 100),
        'world_up': _vec(template.get('WorldAlignmentDirection'), default=0).tolist(),
        'emissive': bool(template.get('Emissive')) or max(max(c) for c in color) > 2.0,
        'texture': texture, 'mesh': mesh[0] if mesh else None, 'materials': mesh[1] if mesh else [],
        'frames': max(int(atlas.get('AnimationFrameCount') or template.get('AnimationFrameCount', 1) or 1), 1),
        'columns': max(int(atlas.get('AnimationColumnCount') or template.get('AnimationFrameColumnCount', 1) or 1), 1),
        'anim_speed': float(anim.get('AnimationSpeed', 0)) if anim else 0.0,
        'anim_on_life': bool(anim.get('BasedOnLifetime')) if anim else False,
        'curves': {'size': e.curve('UpdateSizeData'), 'size_x': e.curve('UpdateSizeXData'),
                   'size_y': e.curve('UpdateSizeYData'), 'alpha': alpha, 'color': color},
        'particles': {
            'spawn': (spawn[alive] + delay).round(4).tolist(), 'life': life[alive].round(4).tolist(),
            'size': size[alive].round(4).tolist(), 'frame': frame[alive].tolist(), 'tint': tint[alive].round(4).tolist(),
            'flip': flip[alive].tolist(), 'track': track[alive].round(4).tolist(), 'angle': angles[alive].round(4).tolist()},
        'notes': notes,
    }
    if 'wind' in extras:  # metres one m/s of world wind has carried each particle, per path point
        out['particles']['wind_track'] = extras['wind'][alive].round(4).tolist()
    if 'end' in extras and (extras['end'][alive] < 1).any():  # killed by a collision this far into its life
        out['particles']['end'] = extras['end'][alive].round(4).tolist()
    if distortion:
        out['distortion'] = distortion
    return out


def _gpu_processors(doc, template):
    """A GPU emitter template as the processor list a CPU emitter would have."""
    def obj(name):
        ref = template.get(name) or {}
        return doc['objects'][ref['index']] if ref.get('$pointer') == 1 else None

    def pointer(name):
        ref = template.get(name) or {}
        return ref if ref.get('$pointer') == 1 else None

    out = [{'$type': 'SpawnRateData', 'SpawnRate': float(template.get('SpawnRate', 0)), 'EvaluatorInput': 1},
           {'$type': 'UpdateAgeData', 'Lifetime': float(template.get('Lifetime', 1)), 'EvaluatorInput': 1},
           {'$type': 'UpdateLinearVelocityData', 'EvaluatorInput': 2},
           {'$type': 'UpdateTextureCoordsData', 'BaseTexture': template.get('BaseTexture'), 'EvaluatorInput': 2},
           {'$type': 'UpdateColorData', 'Color': template.get('Color'), 'EvaluatorInput': 2},
           {'$type': 'LocalForceData', 'LocalForce': template.get('GlobalAcceleration'), 'EvaluatorInput': 2},
           {'$type': 'AirResistanceData', 'DragFactor': float(template.get('ParticleDrag', 0) or 0), 'EvaluatorInput': 2}]
    shape = next((pointer(k) for k in ('SpawnPositionSuperSphere', 'SpawnPositionSphere', 'SpawnPositionRandomXYZ',
                                       'SpawnPositionBox') if pointer(k)), None)
    out.append({'$type': 'SpawnPositionData', 'Pre': shape, 'EvaluatorInput': 1})
    shape = next((pointer(k) for k in ('SpawnDirectionSuperSphere', 'SpawnDirectionSphere', 'SpawnDirectionRandomXYZ')
                  if pointer(k)), None)
    direction = dict(obj('SpawnDirection') or {}, **{'$type': 'SpawnDirectionData', 'Pre': shape, 'EvaluatorInput': 1})
    out.append(direction)
    speed = obj('SpawnSpeed') or {}
    out.append({'$type': 'SpawnSpeedData', 'Speed': float(speed.get('Speed', 0) or 0),
                'Pre': pointer('SpawnSpeedRandomXYZ'), 'EvaluatorInput': 1})
    size = obj('SpawnSize') or {}
    out.append({'$type': 'SpawnSizeData', 'Size': float(size.get('Size', 1) or 1),
                'Pre': pointer('SpawnSizeRandomXYZ'), 'EvaluatorInput': 1})
    if pointer('ParticleSizePolynomial'):
        out.append({'$type': 'UpdateSizeData', 'Pre': pointer('ParticleSizePolynomial'), 'EvaluatorInput': 2})
    transparency = pointer('TransparencyPolynomial') or (obj('Transparency') or {}).get('Pre')
    if transparency:
        out.append({'$type': 'UpdateTransparencyData', 'Pre': transparency, 'EvaluatorInput': 2})
    color = obj('ColorProcessor')
    if color:
        tint = color.get('Pre') if color.get('$type') == 'UpdateColorData' else template.get('ColorProcessor')
        out[4] = {'$type': 'UpdateColorData', 'Pre': tint, 'EvaluatorInput': 2,
                  'Color': color.get('Color') if color.get('$type') == 'UpdateColorData' else template.get('Color')}
    if float(template.get('RotationSpeed', 0) or 0):
        out.append({'$type': 'SpawnRotationSpeedData', 'RotationSpeed': float(template['RotationSpeed']) / (2 * math.pi),
                    'EvaluatorInput': 1})
        out.append({'$type': 'UpdateRotationData', 'EvaluatorInput': 2, 'Pre': pointer('RotationSpeedPolynomial')})
    return out


def gpu_template(template):
    """GPU template fields under the names emitter_data reads from CPU templates."""
    t = dict(template)
    t['Lifetime'] = float(template.get('EmitterLifetime', 0) or 0)
    t['MaxCount'] = int(template.get('MaxParticleCount', 0) or 0)
    t['EmittableType'] = 100
    t['EmittableAlignment'] = 0
    t['$gpu_collision'] = float(template.get('CollisionThickness', 0) or 0)  # collides with the depth buffer
    t['$gpu_damping'] = float(template.get('CollisionDampening', 0) or 0)
    return t


# ---------------- Battle for Neighborville emitter graphs ----------------
class BfnConverter:
    """The converter calls effect_data makes, served by the Battle for Neighborville level converter."""

    def __init__(self, archive, textures, assets):
        import convert_legacy
        self.archive, self.assets = archive, Path(assets)
        self.legacy = convert_legacy.LegacyConverter(archive, textures, assets, 'bfn')
        self.volumes = {}

    def texture(self, guid):
        found = self.archive.by_guid.get(guid)
        if not found or not found[1]['objects']:
            return None
        result, _ = self.legacy.texture_object(found[1]['objects'][0])
        return result[0] if result else None

    def convert_static(self, found, lod):
        return self.legacy.mesh(found[0], found[1])[:2]

    def volume(self, found):
        """A 3D texture (the graphs' vector field) for the shaders to sample."""
        import dxbc
        import emitter_graph
        guid = found[0]['guid']
        if guid not in self.volumes:
            self.volumes[guid] = None
            rid = (found[1].get('Resource') or {}).get('$resource')
            resource = self.archive.resources.get(rid)
            if resource:
                header = (self.archive.root / resource['path']).read_bytes()
                # Exported headers are widened to the reader's layout: the chunk GUID moves from 32 to 36.
                chunk = self.archive.chunks.get((header[36:52] if len(header) >= 144 else header[32:48]).hex())
                if chunk:
                    pixels = emitter_graph.volume_texture(header, (self.archive.root / chunk['path']).read_bytes(),
                                                          _bc_colors, _bc3_alpha)
                    self.volumes[guid] = dxbc.Texture(pixels)
        return self.volumes[guid]


TEXTURE_SKIP = ('normal', 'noise', 'distort', 'flow', 'gradient', 'erosion', 'mask', '_n_', 'vectorfield')
EMISSIVE_HINTS = ('emissive', 'additive', 'blackbody', 'glow', 'fire', 'spark', 'ember', 'flash', 'electric', 'lightning')


def _graph_texture(archive, converter, gdoc, graph, haze=False):
    """The graph's sprite: its first colour texture (its distortion or normal texture for heat haze), and the
    flipbook it is cut into."""
    shader = graph.get('Shader') or {}
    picked = None
    for param in shader.get('TextureParameters', []) or []:
        found = archive.resolve(gdoc, param.get('Value'))
        if not found or found[1].get('$type') != 'TextureAsset':
            continue
        label = ('%s %s' % (param.get('ParameterName', ''), found[1].get('Name', ''))).lower()
        skip = any(k in label for k in TEXTURE_SKIP)
        if haze:
            skip = not any(k in label for k in ('distort', 'normal', '_n_', 'refract'))
        if picked is None or (picked[1] and not skip):
            picked = (found, skip)
    columns, rows = 1, 1
    for param in shader.get('VectorParameters', []) or []:
        if (param.get('ParameterName') or '').lower() in ('flipbooksize', 'flipbook', 'atlassize'):
            v = param.get('Value') or {}
            columns, rows = max(int(round(v.get('x', 1) or 1)), 1), max(int(round(v.get('y', 1) or 1)), 1)
    texture = converter.texture(picked[0][0]['guid']) if picked else None
    return texture, columns, rows


def _euler_tracks(basis, transform):
    """(n, keys, 9) Frostbite emitter-local rotations (left, up, forward axes) -> unwrapped XYZ Euler
    angles of the same rotation in Blender's axes, which the add-on blends between keys."""
    axis = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float)
    m = basis.reshape(basis.shape[:2] + (3, 3)).transpose(0, 1, 3, 2)  # the three axes as columns
    m = m / np.maximum(np.linalg.norm(m, axis=2, keepdims=True), 1e-9)
    m = np.asarray(transform, float)[:3, :3] @ m  # turned with the emitter
    r = axis @ m @ axis.T
    y = np.arcsin(np.clip(-r[..., 2, 0], -1, 1))
    x = np.arctan2(r[..., 2, 1], r[..., 2, 2])
    z = np.arctan2(r[..., 1, 0], r[..., 0, 0])
    return np.unwrap(np.stack([x, y, z], -1), axis=1)


def graph_emitter(archive, converter, doc, entity, index, name):
    """An EmitterGraphEntityData simulated by its graph's own shaders, as a version 2 emitter."""
    import emitter_graph
    found = archive.resolve(doc, entity.get('EmitterGraph'))
    if not found:
        raise ValueError('emitter graph not exported')
    gdoc, graph = found
    graph = dict(graph)
    mode = archive.resolve(gdoc, graph.get('SpawnMode'))
    if mode:
        graph['$spawn_mode'] = mode[1]
    textures = {}
    for key in ('SimulateComputeShaderData', 'SpawnComputeShaderData'):
        for bound in (graph.get(key) or {}).get('RuntimeTextures', []) or []:
            tex = archive.resolve(gdoc, bound.get('Texture'))
            if tex and bound.get('TextureType') == 2:
                volume = converter.volume(tex)
                if volume is not None:
                    textures[bound['BindPoint']] = volume
    sim = emitter_graph.Graph(graph, entity, textures)
    notes = ['simulated from the emitter graph shaders']
    loop = sim.emitter_life <= 0 and not sim.burst
    window = sim.emitter_life if sim.emitter_life > 0 else (LOOP_SECONDS if loop else 0.1)
    seed = int(hashlib.sha1(('%s|%d' % (name, index)).encode()).hexdigest()[:6], 16) / float(1 << 24) * 100.0
    # The ground in the graph's space (its entity places it in the effect), never above the emitter itself.
    m = np.asarray(transform_matrix(entity.get('Transform')), float)
    scale = max(np.linalg.norm(m[1, :3]), 1e-9)
    ground = (m[1, :3] / scale, (min(GROUND, m[1, 3]) - m[1, 3]) / scale)
    out = sim.run(window, loop, seed, max_particles=MAX_PARTICLES, keys=KEYS, ground=ground)
    born = out.get('Position')
    if sim.collides() and born is not None and len(born):
        low = float((born[:, 0, :3] @ ground[0]).min())
        if low < ground[1] - 0.01:  # some spawn below the ground: lower it to them, as for CPU emitters
            sim = emitter_graph.Graph(graph, entity, textures)
            ground = (ground[0], low - 0.01)
            out = sim.run(window, loop, seed, max_particles=MAX_PARTICLES, keys=KEYS, ground=ground)
    n = len(out['spawn'])
    delay = float(entity.get('StartDelay', 0) or 0)

    def field(key, width, default):
        v = out.get(key)
        if v is None or not len(v):
            return None
        v = v[..., :width]
        if v.shape[-1] < width:
            v = np.concatenate([v, np.full(v.shape[:2] + (width - v.shape[-1],), default, np.float32)], -1)
        return np.nan_to_num(v, nan=default, posinf=default, neginf=default)
    position = field('Position', 3, 0.0)
    if position is None:
        position = np.zeros((n, KEYS, 3), np.float32)
    mesh_found = archive.resolve(gdoc, graph.get('Mesh'))
    override = (entity.get('EmitterGraphMesh') or {}).get('Mesh')
    if override:
        mesh_found = archive.resolve(doc, override) or mesh_found
    mesh = None
    if mesh_found and mesh_found[1].get('$type') in ('RigidMeshAsset', 'CompositeMeshAsset'):
        try:
            mesh = converter.convert_static(mesh_found, 0)
        except Exception as error:  # a mesh that does not convert falls back to quads
            notes.append('mesh particle not converted: %s' % error)
    tracks = {}
    if mesh and n and all(field(k, 3, 0.0) is not None for k in ('MatrixLeft', 'MatrixUp', 'MatrixForward')):
        basis = np.concatenate([field(k, 3, 0.0) for k in ('MatrixLeft', 'MatrixUp', 'MatrixForward')], -1)
        lengths = np.linalg.norm(basis.reshape(n, KEYS, 3, 3), axis=-1)  # |left|, |up|, |forward|
        tracks['size_track'] = lengths[..., [0, 2, 1]]  # Blender's X, Y, Z are Frostbite's x, z, y
        tracks['euler_track'] = _euler_tracks(basis, transform_matrix(entity.get('Transform')))
    else:
        scale = field('Scale', 2, 1.0)
        if scale is None:
            scale = field('SpawnSize', 2, 1.0)
        if scale is None:
            scale = np.ones((n, KEYS, 2), np.float32)
        tracks['size_track'] = np.concatenate([scale, scale[..., :1]], -1)
    rotation = field('Rotation', 1, 0.0)
    angle = np.zeros((n, KEYS), np.float32) if rotation is None else rotation[..., 0]
    if rotation is not None and np.abs(angle).max(initial=0) > 2 * math.pi * 1.25:
        angle = np.radians(angle)  # graphs that spin in degrees
    color = field('Color', 3, 1.0)
    if color is None:
        color = np.ones((n, KEYS, 3), np.float32)
    color = np.maximum(color, 0)
    if n and color.max() < 0.02:  # colour set when drawn, not simulated
        color = np.ones_like(color)
        notes.append('colour not simulated (drawn white)')
    alpha = field('Alpha', 1, 1.0)
    alpha = np.ones((n, KEYS), np.float32) if alpha is None else np.clip(alpha[..., 0], 0, 1)
    peak = np.maximum(color.max(-1), 1e-6)
    shader = archive.resolve(gdoc, (graph.get('Shader') or {}).get('Shader'))
    shader_name = (shader[1].get('Name') or '').lower() if shader else ''
    hinted = any(h in (shader_name + ' ' + name).lower() for h in EMISSIVE_HINTS)
    emissive = hinted or (bool(n) and float(np.percentile(peak, 90)) > 1.5)
    tracks['color_track'] = color / np.maximum(peak, 1.0)[..., None]
    tracks['glow_track'] = np.where(peak > 1.0, 0.8 * np.sqrt(peak), 1.0)
    tracks['alpha_track'] = alpha
    haze = distortion_strength(name, shader_name)
    texture, columns, rows = _graph_texture(archive, converter, gdoc, graph, bool(haze))
    frames = columns * rows
    anim = field('Animation', 1, 0.0)
    if anim is not None and frames > 1:
        cell = anim[..., 0]
        tracks['cell_track'] = cell * frames if cell.max(initial=0) <= 1.0 else cell
    if sim.burst:
        notes.append('burst of %d' % sim.max_count)
    duration = window if loop or sim.emitter_life > 0 else 0.0
    particles = {
        'spawn': (out['spawn'] + delay).round(4).tolist(), 'life': out['life'].round(4).tolist(),
        'size': [1.0] * n, 'frame': [0.0] * n, 'tint': [[1.0, 1.0, 1.0]] * n, 'flip': [[0.0, 0.0]] * n,
        'track': position.round(4).tolist(), 'angle': np.asarray(angle).round(4).tolist()}
    for key, value in tracks.items():
        particles[key] = np.asarray(value, np.float32).round(4).tolist()
    return {
        'name': name, 'kind': 'mesh' if mesh else 'quad', 'transform': transform_matrix(entity.get('Transform')),
        'delay': delay, 'loop': loop, 'duration': duration, 'alignment': 0, 'align': 'camera', 'stretch': 0.0,
        'stretch_clamp': 100.0, 'stretch_relative_clamp': 100.0, 'world_up': [0.0, 1.0, 0.0], 'emissive': bool(emissive),
        'texture': texture, 'soft': texture is None, 'mesh': mesh[0] if mesh else None,
        'materials': mesh[1] if mesh else [], 'frames': frames, 'columns': columns, 'anim_speed': 0.0, 'anim_on_life': False,
        'curves': {'size': None, 'size_x': None, 'size_y': None, 'alpha': None, 'color': [[1.0, 1.0, 1.0]] * SAMPLES},
        'particles': particles, 'notes': notes + sim.notes,
    } | ({'distortion': haze} if haze else {})


def transform_matrix(transform):
    from convert import matrix
    return matrix(transform or {}).tolist()


LIGHTS = ('LightEffectEntityData', 'PbrSphereLightEntityData', 'PointLightEntityData', 'SpotLightEntityData',
          'PbrSpotLightEntityData', 'PbrRectangularLightEntityData')
PHYSICAL_LIGHT = 1e4  # brighter than this, a light's intensity is in physical units (Battle for Neighborville)


def _kelvin(temperature):
    """Approximate RGB (0..1) of a black body at `temperature` kelvin (Tanner Helland's fit)."""
    t = max(float(temperature), 1000.0) / 100.0
    r = 1.0 if t <= 66 else min(max(1.292936186 * (t - 60) ** -0.1332047592, 0), 1)
    g = (0.39008157876 * math.log(t) - 0.63184144378) if t <= 66 else 1.129890861 * (t - 60) ** -0.0755148492
    b = 1.0 if t >= 66 else (0.0 if t <= 19 else 0.54320678911 * math.log(t - 10) - 1.19625408914)
    return [r, min(max(g, 0), 1), min(max(b, 0), 1)]


def light_data(archive, doc, entity):
    """A light of an effect: where it is, its colour and reach, and its intensity over its life. A
    LightEffectEntityData wraps the light it shows with a lifetime, an intensity curve (clamped to
    IntensityMin..Max), a random flicker (RandomIntensityMin..Max) and looping."""
    found = archive.resolve(doc, entity.get('Light'))
    light = found[1] if found else entity
    color = _vec(light.get('Color'), default=1)
    if light.get('LightColorMode') == 1 and light.get('Temperature'):
        color = np.asarray(_kelvin(light['Temperature'])) * max(float(color.max()), 1.0)
    transform = np.asarray(transform_matrix(entity.get('Transform')), float)
    if light is not entity and light.get('Transform'):
        transform = transform @ np.asarray(transform_matrix(light.get('Transform')), float)
    curve = entity.get('IntensityCurve')
    lo, hi = float(entity.get('IntensityMin', 0) or 0), float(entity.get('IntensityMax', 0) or 0)
    values = None
    if curve:
        values = _poly(curve, np.linspace(0, 1, SAMPLES))
        values = np.clip(values, lo, hi) if hi > lo else np.clip(values, 0, 1e3)
    kind = light.get('$type', '')
    intensity = float(light.get('Intensity', 1) or 0)
    out = {'type': kind, 'transform': transform.tolist(), 'color': color.tolist(),
           'intensity': intensity * float(light.get('Dimmer', 1) or 1),
           'radius': float(light.get('Radius', light.get('AttenuationRadius', 5)) or 5),
           'size': float(light.get('SphereRadius', light.get('Width', 0)) or 0),
           'lifetime': float(entity.get('Lifetime', 0) or 0), 'delay': float(entity.get('StartDelay', 0) or 0),
           'looping': bool(entity.get('Looping')),
           'curve': [float(v) for v in values] if values is not None else None,
           'physical': intensity >= PHYSICAL_LIGHT}
    flicker = float(entity.get('RandomIntensityMin', 1) or 0), float(entity.get('RandomIntensityMax', 1) or 0)
    if 'RandomIntensityMin' in entity and abs(flicker[1] - flicker[0]) > 1e-3:
        out['flicker'] = [min(flicker), max(flicker)]
    if 'Spot' in kind or 'Rectangular' in kind:
        outer = float(light.get('OuterAngle', light.get('ConeOuterAngle', 40)) or 40)
        inner = float(light.get('InnerAngle', light.get('ConeInnerAngle', 0)) or 0)
        out['spot'] = {'outer': outer, 'inner': min(inner, outer)}
    return out


def _shader_name(archive, doc, template):
    """The surface shader an emitter template draws with (its path), or ''."""
    ref = (template.get('Shader') or {}).get('Shader') or {}
    found = archive.resolve(doc, ref) if ref else None
    if found:
        return (found[0].get('name') or found[1].get('Name') or '').lower()
    if ref.get('$pointer') == 2:
        return NAMES.get(doc['imports'][ref['index']]['file'], '').lower()
    return ''


# Effect parts with no Blender counterpart yet, and what they do in the game.
UNPORTED = {'VisualEnvironmentEffectEntityData': 'screen flash (visual environment change) not ported',
            'LocationEffectEntityData': 'beam end point (location effect) not ported',
            'DecalEffectEntityData': 'decal not ported', 'CameraShakeEffectEntityData': 'camera shake not ported',
            'ForceFeedbackEffectEntityData': 'controller rumble not ported'}
DISTORTION = ('distortion', 'refraction', 'heathaze', 'heat_haze', '_distort')


def distortion_strength(*names):
    """How strongly an emitter bends what is behind it, from its own or its shader's name (0: it does not)."""
    text = ' '.join(n.lower() for n in names if n)
    if not any(k in text for k in DISTORTION) and not re.search(r'_heat(burst)?(_|$)', text):
        return 0.0
    return 1.5 if 'shockwave' in text or 'radial' in text else 1.0


NAMES = {}   # file GUID -> name, for the files an export read but did not keep (shaders, sounds)
SOUNDS = {}  # sound file GUID -> exported WAV and how it plays (filled by convert)


def sound_data(doc, entity):
    """A SoundEffectEntityData as {'wav', 'name', 'transform', 'delay', 'loop', 'loudness'}, or a note (str)."""
    ref = entity.get('Sound') or {}
    if ref.get('$pointer') != 2:
        return 'sound has no asset'
    found = SOUNDS.get(doc['imports'][ref['index']]['file'])
    if not found:
        return 'sound not exported'
    if 'wav' not in found:
        return 'sound %s' % found.get('error', 'not decoded')
    return {'wav': found['wav'], 'name': found['name'].split('/')[-1], 'transform': transform_matrix(entity.get('Transform')),
            'delay': float(entity.get('StartDelay', 0) or 0), 'loop': bool(found.get('loop')),
            'loudness': float(found.get('loudness') or 0)}


def effect_data(archive, converter, doc, name, seen=None):
    seen = (seen or set()) | {doc['guid']}
    root = archive.resolve(doc, doc['objects'][0].get('Object'))
    # version 2: particles carry their recorded paths ('track', 'angle'); version 1 files held start
    # position and velocity, and the add-on still plays those.
    out = {'name': name, 'version': 2, 'emitters': [], 'lights': [], 'sounds': [], 'notes': []}
    if not root:
        out['notes'].append('no effect entity')
        return out
    for i, pointer in enumerate(root[1].get('Components', [])):
        found = archive.resolve(doc, pointer)
        if not found:
            continue
        entity = found[1]
        kind = entity['$type']
        if kind in ('EmitterEntityData', 'GpuEmitterEntityData'):
            emitter = archive.resolve(doc, entity.get('Emitter') or entity.get('GpuEmitter') or entity.get('EmitterDocument'))
            if not emitter:
                out['notes'].append('emitter %d not exported' % i)
                continue
            edoc, eobj = emitter
            gpu = eobj['$type'] == 'GpuRuntimeEmitterDocument'
            if eobj['$type'] != 'ScalableEmitterDocument' and not gpu:
                out['notes'].append('%s not ported' % eobj['$type'].replace('Document', ''))
                continue
            template = archive.resolve(edoc, eobj.get('TemplateData') if gpu else
                                       eobj.get('TemplateDataHigh') or eobj.get('TemplateDataUltra'))
            if not template:
                continue
            ename = next((a['name'] for a, d in archive.documents.values() if d is edoc), 'emitter_%d' % i)
            if isinstance(converter, BfnConverter) and ename.lower().endswith('/gw2_emitterdocument'):
                note = 'placeholder emitters skipped'  # every classic emitter in BFN points at this one stub
                if note not in out['notes']:
                    out['notes'].append(note)
                continue
            haze = distortion_strength(ename, _shader_name(archive, edoc, template[1]))
            seed = int(hashlib.sha1(('%s|%d' % (ename, i)).encode()).hexdigest()[:8], 16)
            try:
                if gpu:
                    data = emitter_data(archive, converter, edoc, gpu_template(template[1]), ename.split('/')[-1],
                                        entity.get('Transform'), float(entity.get('StartDelay', 0) or 0), seed,
                                        _gpu_processors(edoc, template[1]), gpu=True, distortion=haze)
                    data['emissive'] = data['emissive'] or float(template[1].get('EmissiveExposureFactor', 0) or 0) > 0
                else:
                    data = emitter_data(archive, converter, edoc, template[1], ename.split('/')[-1],
                                        entity.get('Transform'), float(entity.get('StartDelay', 0) or 0), seed,
                                        distortion=haze)
                out['emitters'].append(data)
            except Exception as error:  # one emitter must not lose the effect
                out['notes'].append('%s: %s' % (ename.split('/')[-1], error))
        elif kind == 'EmitterGraphEntityData':
            found = archive.resolve(doc, entity.get('EmitterGraph'))
            gname = next((a['name'] for a, d in archive.documents.values() if found and d is found[0]), 'graph_%d' % i)
            try:
                out['emitters'].append(graph_emitter(archive, converter, doc, entity, i, gname.split('/')[-1]))
            except Exception as error:  # one emitter must not lose the effect
                out['notes'].append('%s: %s' % (gname.split('/')[-1], error))
        elif kind in LIGHTS:
            out['lights'].append(light_data(archive, doc, entity))
        elif kind == 'SoundEffectEntityData':
            sound = sound_data(doc, entity)
            if isinstance(sound, dict):
                out['sounds'].append(sound)
            elif sound not in out['notes']:
                out['notes'].append(sound)
        else:
            note = UNPORTED.get(kind, '%s not ported' % kind)
            if note not in out['notes']:
                out['notes'].append(note)
    out['duration'] = max([e['delay'] + e['duration'] + (max(e['particles']['life']) if e['particles']['life'] else 0)
                           for e in out['emitters']] + [l['delay'] + l['lifetime'] for l in out['lights']] + [0.5])
    out['loop'] = any(e['loop'] for e in out['emitters'])
    return out


def _reach(data):
    """How far an effect's particles get from its centre, sideways (to space several effects apart)."""
    reach = 0.5
    for e in data['emitters']:
        track = e['particles'].get('track')
        if track:
            t = np.asarray(e['transform'])
            pts = np.asarray(track).reshape(-1, 3) @ t[:3, :3].T + t[:3, 3]
            reach = max(reach, float(np.percentile(np.abs(pts[:, [0, 2]]), 95)))
    return min(reach, 30.0)


# ---------------- conversion ----------------
def convert(export_dir, textures, assets, out, gap=1.0):
    export_dir, out, assets = Path(export_dir), Path(out), Path(assets)
    inventory = json.loads((export_dir / 'inventory.json').read_text(encoding='utf-8'))
    if inventory.get('profile') == 'bfn':
        from convert import Archive
        archive = Archive(export_dir)
        converter = BfnConverter(archive, textures, assets)
    else:
        import gw2_convert
        archive = gw2_convert.Gw2Archive(export_dir)
        converter = gw2_convert.Gw2Converter(archive, textures, assets)
    NAMES.update(inventory.get('names') or {})
    SOUNDS.clear()
    for guid, sound in (inventory.get('sounds') or {}).items():
        if sound.get('wav') and (export_dir / sound['wav']).is_file():
            data = (export_dir / sound['wav']).read_bytes()
            rel = 'sounds/frostbite/%s.wav' % hashlib.sha1(data).hexdigest()[:16]
            (assets / rel).parent.mkdir(parents=True, exist_ok=True)
            (assets / rel).write_bytes(data)
            sound = dict(sound, wav=rel)
        else:
            sound = {k: v for k, v in sound.items() if k != 'wav'}
        SOUNDS[guid] = sound
    wanted = {i['asset'].lower(): i for i in inventory.get('items', [])}
    places, errors = [], []
    x = 0.0
    for name, (asset, doc) in archive.documents.items():
        if name.lower() not in wanted or asset.get('root_type') != 'EffectBlueprint':
            continue
        item = wanted[name.lower()]
        try:
            data = effect_data(archive, converter, doc, item['name'])
        except Exception as error:
            errors.append({'stage': 'effect', 'name': name, 'error': str(error)})
            continue
        if not data['emitters'] and not data['lights']:
            errors.append({'stage': 'effect', 'name': name, 'error': 'nothing to show: ' + '; '.join(data['notes'][:3])})
            continue
        rel = 'effects/frostbite/%s.json' % hashlib.sha1(name.lower().encode()).hexdigest()[:16]
        (assets / rel).parent.mkdir(parents=True, exist_ok=True)
        (assets / rel).write_text(json.dumps(data), encoding='utf-8')
        reach = _reach(data)
        x += reach if places else 0.0
        places.append({'kind': 'frostbite_effect', 'name': item['name'],
                       'matrix': [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                       'tags': {'effect': rel, 'source': name, 'level_instance': item['name']}})
        x += reach + gap
    if hasattr(converter, 'finish_textures'):
        converter.finish_textures()
    if not places:
        raise ValueError('No effects converted' + (': ' + errors[0]['error'] if errors else ''))
    scene = {'format': 'aura-frostbite-scene', 'version': 1, 'name': 'Effects', 'places': places}
    report = {'profile': inventory.get('profile'), 'placements': len(places), 'effects': len(places), 'errors': errors}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(scene, indent=1), encoding='utf-8')
    out.with_suffix('.report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    return scene, report


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=('discover', 'export', 'convert'))
    p.add_argument('--game')
    p.add_argument('--out', required=True)
    p.add_argument('--items', help='JSON list of effects from discover')
    p.add_argument('--names', help='unused (BFN type table)')
    p.add_argument('--export')
    p.add_argument('--textures')
    p.add_argument('--assets')
    a = p.parse_args()
    if a.action == 'discover':
        Path(a.out).write_text(json.dumps(discover(a.game), indent=1), encoding='utf-8')
    elif a.action == 'export':
        export(a.game, json.loads(Path(a.items).read_text(encoding='utf-8')), a.out, a.names)
    else:
        scene, report = convert(a.export, a.textures, a.assets, a.out)
        print('Effects:', report['effects'], 'errors:', len(report['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
