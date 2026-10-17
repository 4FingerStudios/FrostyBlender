"""Visual effects (explosions, muzzle flashes, smoke, sparks) from Garden Warfare 1 and 2.

An effect is an EffectBlueprint whose EffectEntityData lists emitters and lights. Each emitter is a
ScalableEmitterDocument: a template (count, lifetime, texture flipbook, mesh) and a chain of
processors that spawn particles (rate, position, direction, speed, size, rotation, colour) and update
them over their life (size, colour, transparency, gravity, drag). Conversion runs the spawn side here,
seeded so the result is repeatable, and writes each particle's start state plus the over-life curves;
the Blender add-on plays them back with Geometry Nodes, so the timeline scrubs without a cache.

Approximated: turbulence, wind, collisions, alpha erosion and stretched (velocity-aligned) quads.
Not ported: ribbons, beams, GPU emitters and sounds (listed in each effect's notes).

    python effects.py discover --game GAME --out effects.json
    python effects.py export --game GAME --items items.json --out EXPORT
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


def discover(game):
    kind = game_kind(game)
    if kind == 'bfn':
        raise ValueError('Effects are not supported for Battle for Neighborville yet')
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
def export(game, items, output):
    kind = game_kind(game)
    if kind == 'bfn':
        raise ValueError('Effects are not supported for Battle for Neighborville yet')
    game, output = Path(game), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'ebx').mkdir(exist_ok=True)
    index = gw2.cached_dependency_index(game)
    by_name = {location[3]['name'].lower(): location for location in index['ebx'].values()}
    inventory = {'profile': kind, 'game': str(game), 'package': 'effects', 'assets': [], 'errors': [],
                 'archive_policy': 'base-only shared index'}
    seen = set()

    def store(location):
        path, offset, size, source = location
        with path.open('rb') as stream:
            stream.seek(offset)
            content = gw2.decompress(gw2.Reader(stream.read(size)), source['original_size'])
        document = gw2.Ebx(content).document()
        if document['guid'] in seen:
            return None
        seen.add(document['guid'])
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
        for ref in document['imports']:
            location = index['ebx'].get(ref['file'])
            if location is None or ref['file'] in seen:
                continue
            found = store(location)
            if found:
                queue.append(found[1])
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
    toc = {}
    if kind == 'gw1':
        for base in (game / 'Data/Win32', game / 'Update/Patch/Data/Win32'):
            for path in sorted(base.glob('*chunks.toc')):
                for chunk in gw2.read_db(path).get('chunks', []):
                    toc[chunk['id']] = (path.with_suffix('.sb'), chunk)
    for asset in list(inventory['assets']):
        if asset.get('resource_type') != ATLAS_RESOURCE or 'bytes' not in asset:
            continue
        header = (output / asset['path']).read_bytes()
        if len(header) < 32:
            continue
        guid = header[16:32].hex()
        if guid in have:
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
        rel = 'chunk/%s.bin' % hashlib.sha1(content).hexdigest()
        (output / rel).parent.mkdir(parents=True, exist_ok=True)
        (output / rel).write_bytes(content)
        inventory['assets'].append({'kind': 'chunk', 'guid': guid, 'name': guid, 'path': rel, 'bytes': len(content),
                                    'original_size': len(content)})
        have.add(guid)


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

    def __init__(self, archive, doc, template, seed, notes):
        self.archive, self.doc, self.template, self.notes = archive, doc, template, notes
        self.rng = np.random.default_rng(seed)
        self.processors = []
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
            # Spreads the up axis into a cone `Angle` degrees wide.
            half = math.radians(min(float(o.get('Angle', 0)), 360.0)) / 2
            cos_t = 1 - self.rng.random(n) * (1 - math.cos(half))
            sin_t = np.sqrt(np.maximum(1 - cos_t ** 2, 0))
            phi = self.rng.random(n) * 2 * math.pi
            return np.stack([sin_t * np.cos(phi), cos_t, sin_t * np.sin(phi)], 1)
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
        rate = self.find('SpawnRateData')
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


def emitter_data(archive, converter, doc, template, name, transform, delay, seed):
    notes = []
    e = Emitter(archive, doc, template, seed, notes)
    loop = _loops(template)
    duration = LOOP_SECONDS if loop else float(template['Lifetime'])
    spawn = e.spawn_times(duration, loop)
    n = len(spawn)
    emitter_t = spawn / max(duration, 1e-6)
    life = e.lifetimes(n)
    keep = e.limit(spawn, life)
    up = _vec(transform.get('up'), default=0) if transform else np.array([0, 1, 0.])

    position = e.vector(e.find('SpawnPositionData'), n, [0, 0, 0])
    direction = e.vector(e.find('SpawnDirectionData'), n, up)
    direction /= np.maximum(np.linalg.norm(direction, axis=1, keepdims=True), 1e-9)
    speed_p = e.find('SpawnSpeedData')
    speed = float(speed_p.get('Speed', 0)) * e.scalar(speed_p, n, emitter_t) if speed_p else np.zeros(n)
    size_p = e.find('SpawnSizeData')
    size = float(size_p.get('Size', 1)) * e.scalar(size_p, n, emitter_t) if size_p else np.ones(n)
    rot_p, spin_p = e.find('SpawnRotationData'), e.find('SpawnRotationSpeedData')
    rotation = float(rot_p.get('Rotation', 0)) * e.scalar(rot_p, n) * 2 * math.pi if rot_p else np.zeros(n)
    spin = float(spin_p.get('RotationSpeed', 0)) * e.scalar(spin_p, n) * 2 * math.pi if spin_p else np.zeros(n)
    if spin_p and e.find('UpdateRotationData') is None:
        spin = np.zeros(n)
    frame_p = e.find('SpawnAnimationFrameData')
    frame = np.floor(float(frame_p.get('AnimationFrame', 0)) * e.scalar(frame_p, n)) if frame_p else np.zeros(n)
    tint_p = e.find('SpawnColorRandomData')
    if tint_p:
        f = e.scalar(tint_p, n)[:, None]
        tint = _vec(tint_p.get('Color0'), default=1) * (1 - f) + _vec(tint_p.get('Color1'), default=1) * f
    else:
        tint = np.ones((n, 3))

    gravity, drag = np.zeros(3), 0.0
    g = e.find('GravityData')
    if g:
        gravity[1] -= float(g.get('Gravity', 0))
    for force in (p for p in e.processors if p['$type'] == 'LocalForceData'):
        gravity += _vec(force.get('LocalForce'))
    a = e.find('AirResistanceData')
    if a:
        drag = max(float(a.get('DragFactor', 0)), 0.0)
    if e.find('UpdateLinearVelocityData') is None:
        speed = np.zeros(n)
        gravity[:] = 0
    for kind in ('TurbulanceData', 'WorldWindData', 'UpdateCollisionData'):
        if e.find(kind):
            e.note(kind.replace('Data', '') + ' not simulated')

    color = e.curve('UpdateColorData')
    base_color = _vec((e.find('UpdateColorData') or {}).get('Color'), default=1)
    if color is None:
        color = [base_color.tolist()] * SAMPLES
    else:
        color = (np.asarray(color) * base_color).tolist()
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
    kind = {100: 'quad', 200: 'mesh', 300: 'ribbon', 400: 'beam'}.get(template.get('EmittableType'), 'quad')
    if mesh:
        kind = 'mesh'
    if kind in ('ribbon', 'beam'):
        notes.append('%s drawn as quads' % kind)

    alive = keep
    return {
        'name': name, 'kind': kind, 'transform': transform_matrix(transform), 'delay': delay, 'loop': loop,
        'duration': duration, 'alignment': template.get('EmittableAlignment', 0),
        'world_up': _vec(template.get('WorldAlignmentDirection'), default=0).tolist(),
        'emissive': bool(template.get('Emissive')) or max(max(c) for c in color) > 2.0,
        'texture': texture, 'mesh': mesh[0] if mesh else None, 'materials': mesh[1] if mesh else [],
        'frames': max(int(atlas.get('AnimationFrameCount') or template.get('AnimationFrameCount', 1) or 1), 1),
        'columns': max(int(atlas.get('AnimationColumnCount') or template.get('AnimationFrameColumnCount', 1) or 1), 1),
        'anim_speed': float(anim.get('AnimationSpeed', 0)) if anim else 0.0,
        'anim_on_life': bool(anim.get('BasedOnLifetime')) if anim else False,
        'gravity': gravity.tolist(), 'drag': drag,
        'curves': {'size': e.curve('UpdateSizeData'), 'size_x': e.curve('UpdateSizeXData'),
                   'size_y': e.curve('UpdateSizeYData'), 'alpha': e.curve('UpdateTransparencyData'),
                   'color': color},
        'particles': {
            'spawn': (spawn[alive] + delay).round(4).tolist(), 'life': life[alive].round(4).tolist(),
            'position': position[alive].round(4).tolist(), 'velocity': (direction * speed[:, None])[alive].round(4).tolist(),
            'size': size[alive].round(4).tolist(), 'rotation': rotation[alive].round(4).tolist(),
            'spin': spin[alive].round(4).tolist(), 'frame': frame[alive].tolist(), 'tint': tint[alive].round(4).tolist()},
        'notes': notes,
    }


def transform_matrix(transform):
    from convert import matrix
    return matrix(transform or {}).tolist()


def light_data(archive, doc, entity):
    light = archive.resolve(doc, entity.get('Light'))
    light = light[1] if light else entity
    color = _vec(light.get('Color'), default=1)
    curve = entity.get('IntensityCurve')
    return {'transform': transform_matrix(entity.get('Transform')), 'color': color.tolist(),
            'intensity': float(light.get('Intensity', 1)), 'radius': float(light.get('Radius', light.get('AttenuationRadius', 5)) or 5),
            'lifetime': float(entity.get('Lifetime', 0) or 0), 'delay': float(entity.get('StartDelay', 0) or 0),
            'looping': bool(entity.get('Looping')),
            'curve': [float(v) for v in np.clip(_poly(curve, np.linspace(0, 1, SAMPLES)), 0, 1e3)] if curve else None}


def effect_data(archive, converter, doc, name, seen=None):
    seen = (seen or set()) | {doc['guid']}
    root = archive.resolve(doc, doc['objects'][0].get('Object'))
    out = {'name': name, 'emitters': [], 'lights': [], 'notes': []}
    if not root:
        out['notes'].append('no effect entity')
        return out
    for i, pointer in enumerate(root[1].get('Components', [])):
        found = archive.resolve(doc, pointer)
        if not found:
            continue
        entity = found[1]
        kind = entity['$type']
        if kind == 'EmitterEntityData':
            emitter = archive.resolve(doc, entity.get('Emitter'))
            if not emitter:
                out['notes'].append('emitter %d not exported' % i)
                continue
            edoc, eobj = emitter
            if eobj['$type'] != 'ScalableEmitterDocument':
                out['notes'].append('%s not ported' % eobj['$type'].replace('Document', ''))
                continue
            template = archive.resolve(edoc, eobj.get('TemplateDataHigh') or eobj.get('TemplateDataUltra'))
            if not template:
                continue
            ename = next((a['name'] for a, d in archive.documents.values() if d is edoc), 'emitter_%d' % i)
            if 'distortion' in ename.lower() or 'refraction' in ename.lower():
                out['notes'].append('%s: heat distortion not ported' % ename.split('/')[-1])
                continue
            seed = int(hashlib.sha1(('%s|%d' % (ename, i)).encode()).hexdigest()[:8], 16)
            try:
                out['emitters'].append(emitter_data(archive, converter, edoc, template[1], ename.split('/')[-1],
                                                    entity.get('Transform'), float(entity.get('StartDelay', 0) or 0), seed))
            except Exception as error:  # one emitter must not lose the effect
                out['notes'].append('%s: %s' % (ename.split('/')[-1], error))
        elif kind in ('LightEffectEntityData', 'PbrSphereLightEntityData', 'PointLightEntityData', 'SpotLightEntityData'):
            out['lights'].append(light_data(archive, doc, entity))
        elif kind == 'SoundEffectEntityData':
            out['notes'].append('sound not ported')
        else:
            out['notes'].append('%s not ported' % kind)
    out['duration'] = max([e['delay'] + e['duration'] + (max(e['particles']['life']) if e['particles']['life'] else 0)
                           for e in out['emitters']] + [l['delay'] + l['lifetime'] for l in out['lights']] + [0.5])
    out['loop'] = any(e['loop'] for e in out['emitters'])
    return out


# ---------------- conversion ----------------
def convert(export_dir, textures, assets, out, gap=4.0):
    import gw2_convert
    export_dir, out, assets = Path(export_dir), Path(out), Path(assets)
    inventory = json.loads((export_dir / 'inventory.json').read_text(encoding='utf-8'))
    archive = gw2_convert.Gw2Archive(export_dir)
    converter = gw2_convert.Gw2Converter(archive, textures, assets)
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
        places.append({'kind': 'frostbite_effect', 'name': item['name'],
                       'matrix': [[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                       'tags': {'effect': rel, 'source': name, 'level_instance': item['name']}})
        x += gap
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
        export(a.game, json.loads(Path(a.items).read_text(encoding='utf-8')), a.out)
    else:
        scene, report = convert(a.export, a.textures, a.assets, a.out)
        print('Effects:', report['effects'], 'errors:', len(report['errors']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
