"""Battle for Neighborville EmitterGraph particles, simulated by running the graph's own shaders.

An EmitterGraph is a node graph compiled into a spawn and a simulate compute shader (DXBC). The engine
gives both a 1408-byte record per emitter: its transform, clocks and particle ring buffer, then the
graph's exposed parameters at 256 + 16 * i. Particles live in one raw buffer laid out as the graph's
RuntimeParticleDataBuffers list, packed in order. This module plays the engine's part frame by frame
(dxbc.py runs the shaders) and records each particle's named values over its life.

Engine record fields, read off how the shaders use them:
    0, 16, 32   emitter-to-world rows (3x4)        64, 80, 96  the previous frame's rows
    192, 196    emitter age, emitter lifespan       200, 204    random seed, particle lifespan
    208..216    ring start, alive count, capacity   224, 228    force/exclusion volume range
    240, 244    particle buffer base, stride        252         parent particle (child emitters)
Graphs that collide sample the depth buffer (t12) through the view (t10): its view-projection rows at 0,
their inverse at 64, the camera at 240 and one texel's size in clip space at 896, with reversed depth (near
is 1). Given a ground plane, those graphs see an orthographic view straight down onto it and a depth buffer
holding it, so they bounce off it with their own restitution; otherwise the view puts every point off
screen and nothing collides.
"""
import struct

import numpy as np

import dxbc

RECORD = 1408
PARAMS = 256
# RuntimeParticleDataBuffer.BufferType -> bytes: float, float2, float3, float4, half2, half4, 2 x uint, float3x3.
SIZES = {0: 4, 1: 8, 2: 12, 3: 16, 4: 4, 5: 8, 6: 8, 7: 36}
HALF = {4: 2, 5: 4}
NO_PARENT = 0x7FFFFFFF


def quality(value, default=0.0):
    """QualityScalable{Float,Int,Bool} at High."""
    if isinstance(value, dict):
        return value.get('High', value.get('Ultra', default))
    return default if value is None else value


def layout(graph):
    """{name: (offset, type)} of the particle record, and its stride."""
    out, at = {}, 0
    for b in graph.get('RuntimeParticleDataBuffers', []):
        out[b['VariableName']] = (at, b['BufferType'])
        at += SIZES.get(b['BufferType'], 4)
    return out, at


def read_field(words, offset, kind):
    """One named value of every particle, (n, components) float32, from (n, stride/4) uint32 records."""
    first = offset // 4
    if kind in HALF:
        count = HALF[kind] // 2
        raw = words[:, first:first + count]
        lo, hi = (raw & 0xFFFF).astype(np.uint16), (raw >> 16).astype(np.uint16)
        halves = np.stack([lo, hi], 2).reshape(len(words), -1)
        return halves.view(np.float16).astype(np.float32)
    count = SIZES.get(kind, 4) // 4
    return words[:, first:first + count].view(np.float32)


class Graph:
    """One EmitterGraph with an entity's overrides, ready to simulate."""

    def __init__(self, graph, entity=None, textures=None):
        self.g = graph
        self.spawn = dxbc.parse(bytes(graph['SpawnComputeShaderData']['CompiledComputeShader']))
        self.simulate = dxbc.parse(bytes(graph['SimulateComputeShaderData']['CompiledComputeShader']))
        self.fields, self.stride = layout(graph)
        if self.stride != graph.get('ParticleDataByteStride', self.stride):
            raise ValueError('Particle layout does not match its stride')
        entity = entity or {}
        overrides = entity.get('EmitterGraphOverrides') or {}

        def pick(name, base):
            return quality(overrides.get(name)) if overrides.get('Is%sOverrideSet' % name) else base
        mode = graph.get('$spawn_mode') or {}
        self.burst = mode.get('$type') == 'SpawnModeBurst'
        self.life = float(pick('ParticleLifeSpan', quality(graph.get('ParticleLifeSpan'), 1.0)))
        self.emitter_life = float(pick('EmitterLifeSpan', quality(graph.get('EmitterLifeSpan'), 0.0)))
        self.rate = float(pick('SpawnRate', quality(mode.get('SpawnRate'), 0.0)))
        self.max_count = int(pick('ParticleMaxCount', quality(mode.get('ParticleMaxCount'), 64)))
        # Exposed parameters: the graph's defaults, then the entity's values for the same property.
        values = {p['PropertyId']: p for p in entity.get('EmitterGraphParams', [])}
        self.params = []
        for p in graph.get('EmitterGraphParams', []):
            mine = values.get(p['PropertyId'], p)
            self.params.append(mine)
        self.textures = textures or {}
        self.notes = []

    def record(self, base, t, capacity, start, count, seed, emitter_life):
        r = np.zeros(RECORD // 4, np.uint32)
        f = r.view(np.float32)
        identity = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], np.float32)
        f[0:12] = identity.ravel()
        f[16:28] = identity.ravel()
        f[48] = t
        f[49] = emitter_life
        f[50] = seed
        f[51] = self.life
        r[52], r[53], r[54] = start, count, capacity
        r[56], r[57] = 0, 0
        r[60], r[61] = base, self.stride
        r[63] = 0
        for i, p in enumerate(self.params):
            at = (PARAMS + 16 * i) // 4
            if at + 4 > len(r):
                break
            v = p.get('Value') or {}
            f[at:at + 4] = [float(v.get(c, 0.0)) for c in 'xyzw']
            if p.get('ExposableType') == 5:  # integer choices
                r[at:at + 4] = np.uint32(int(p.get('IntValue', 0)) & 0xFFFFFFFF)
        return r

    def collides(self):
        """Whether the simulate shader reads the depth buffer (t12), i.e. its particles collide."""
        return any(ins.op in (dxbc.OP['GATHER4'], dxbc.OP['SAMPLE'], dxbc.OP['SAMPLE_L'], dxbc.OP['LD'])
                   and any(o.type == dxbc.RESOURCE and o.index and o.index[0][0] == DEPTH for o in ins.operands)
                   for ins in self.simulate['code'])

    def run(self, duration, loop, seed=1.0, fps=30.0, max_particles=4000, keys=17, ground=None):
        """Simulate; returns per-particle keyed values {'spawn', 'life', name: (n, keys, c)}.
        `ground` (up, height): a plane in emitter space that colliding graphs bounce off."""
        capacity = max(1, min(self.max_count, max_particles))
        words = np.zeros(capacity * self.stride // 4, np.uint32)
        dt = 1.0 / fps
        emitter_life = self.emitter_life if self.emitter_life > 0 else duration
        spawn_until = emitter_life if (self.emitter_life > 0 or loop) else duration
        end = spawn_until + self.life * 2.0 + dt
        view = _view()
        frame_constants = np.zeros(176 // 4, np.uint32)
        bindings_tex = dict(self.textures)
        bindings_tex.setdefault(DEPTH, dxbc.Texture(np.ones((1, 1, 4), np.float32)))
        sim_view, sim_tex = view, bindings_tex
        if ground is not None and self.collides():
            sim_view = _ground_view(*ground)
            depth = np.full((GROUND_TEXELS, GROUND_TEXELS, 4), GROUND_DEPTH, np.float32)
            sim_tex = {**bindings_tex, DEPTH: dxbc.Texture(depth, clamp=True)}
            self.notes.append('collides with the ground')
        start, count, owed, t = 0, 0, 0.0, 0.0
        alive_id = np.full(capacity, -1, np.int64)
        born, died = [], []
        names = [n for n in self.fields if n in WANTED or n.startswith('Matrix')]
        lifetime = self.fields.get('Lifetime')
        if lifetime is None:
            raise ValueError('Emitter graph has no Lifetime')
        frames = []  # (time, particle ids, {name: values}) of the particles alive after each step
        burst_done, steps = False, 0
        while t < end and steps < 20000:
            steps += 1
            # Spawn: a burst fills the ring once; a continuous emitter spawns its rate.
            if self.burst:
                want = 0 if burst_done else capacity
                burst_done = True
            elif t < spawn_until:
                owed += self.rate * dt
                want = int(owed)
                owed -= want
            else:
                want = 0
            want = min(want, capacity - count)
            if want > 0:
                first = (start + count) % capacity
                rec = self.record(0, t, capacity, start, count, seed, emitter_life)
                cb = np.zeros((2, 4), np.uint32)
                cb[0] = [first, want, 0, NO_PARENT]
                cb[1].view(np.float32)[:] = [0.0, 0.0, 0.5, 0.0]
                dxbc.run(self.spawn, want, {'cb': {0: cb}, 'buffers': {4: (rec, RECORD), 10: (view, 944),
                                                                      11: (frame_constants, 176)},
                                           'uavs': {0: words}, 'textures': bindings_tex})
                alive_id[(first + np.arange(want)) % capacity] = len(born) + np.arange(want)
                born.extend([t] * want)
                died.extend([None] * want)
                count += want
            # Simulate every slot of the ring.
            if count:
                frame_constants.view(np.float32)[0] = t
                rec = self.record(0, t, capacity, start, count, seed, emitter_life)
                cb = np.zeros((2, 4), np.uint32)
                cb[0].view(np.float32)[:] = [dt, fps, 0.0, 0.0]
                cb[1] = [0, NO_PARENT, 0, 0]
                dxbc.run(self.simulate, count, {'cb': {0: cb}, 'buffers': {4: (rec, RECORD), 10: (sim_view, 944),
                                                                         11: (frame_constants, 176),
                                                                         5: (np.zeros(16, np.uint32), 64),
                                                                         8: (np.zeros(4, np.uint32), 4)},
                                               'uavs': {0: words}, 'textures': sim_tex})
            t += dt
            # Record the living, retire the dead.
            slots = (start + np.arange(count)) % capacity
            pids = alive_id[slots]
            recs = words.reshape(capacity, -1)[slots]
            left = read_field(recs, *lifetime)[:, 0]
            age = t - np.array([born[p] if p >= 0 else t for p in pids])
            gone = (pids < 0) | ~np.isfinite(left) | (left <= 0) | (age > self.life * 4 + 1)
            for p in pids[gone & (pids >= 0)]:
                if died[p] is None:
                    died[p] = t
            live = ~gone & np.array([p >= 0 and died[p] is None for p in pids], bool)
            if live.any():
                frames.append((t, pids[live], {n: read_field(recs[live], *self.fields[n]).copy() for n in names}))
            alive_id[slots[gone]] = -1
            while count and alive_id[start] < 0:
                start = (start + 1) % capacity
                count -= 1
            if not count and t >= spawn_until and (burst_done or not self.burst):
                break
        return _keyed(born, died, frames, names, keys, t)


WANTED = {'Position', 'Scale', 'Rotation', 'Color', 'Alpha', 'Velocity', 'Animation', 'SpawnSize', 'AlignedDirection'}


def _keyed(born, died, frames, names, keys, now):
    """Each particle's recorded values resampled to `keys` points spread over its life."""
    out = {'spawn': np.zeros(0, np.float32), 'life': np.zeros(0, np.float32)}
    for n in names:
        out[n] = np.zeros((0, keys, 1), np.float32)
    if not frames:
        return out
    times = np.concatenate([np.full(len(p), t) for t, p, _ in frames])
    pids = np.concatenate([p for _, p, _ in frames])
    order = np.lexsort((times, pids))
    times, pids = times[order], pids[order]
    values = {n: np.concatenate([v[n] for _, _, v in frames])[order] for n in names}
    cuts = np.flatnonzero(np.diff(pids)) + 1
    groups = np.split(np.arange(len(pids)), cuts)
    spawn, life, keyed = [], [], {n: [] for n in names}
    for g in groups:
        if len(g) < 2:
            continue
        pid = pids[g[0]]
        start = born[pid]
        end = died[pid] if died[pid] is not None else now
        t = times[g]
        want = np.linspace(t[0], t[-1], keys)
        spawn.append(start)
        life.append(max(end - start, 1e-3))
        for n in names:
            v = values[n][g]
            keyed[n].append(np.stack([np.interp(want, t, v[:, c]) for c in range(v.shape[1])], 1))
    if not spawn:
        return out
    out['spawn'], out['life'] = np.array(spawn, np.float32), np.array(life, np.float32)
    for n in names:
        out[n] = np.array(keyed[n], np.float32)
    return out


def _view():
    """t10: a view whose projection puts every point off screen (no depth collision), camera at (0, 2, 8)."""
    v = np.zeros(944 // 4, np.uint32)
    f = v.view(np.float32)
    proj = np.zeros((4, 4), np.float32)
    proj[0, 3], proj[2, 3], proj[3, 3] = 10.0, 0.5, 1.0
    f[0:16] = proj.ravel()
    f[16:32] = np.eye(4, dtype=np.float32).ravel()
    f[60:63] = [0.0, 2.0, 8.0]
    f[224:226] = [1920.0, 1080.0]
    return v


DEPTH = 12            # the depth buffer's texture slot
GROUND_TEXELS = 64
GROUND_DEPTH = 0.5    # the ground's depth in the downward view: halfway between its near and far planes
GROUND_ABOVE, GROUND_FAR, GROUND_EXTENT = 50.0, 100.0, 60.0


def _ground_view(up, height):
    """t10: an orthographic view straight down `up` onto the plane up . p = height (emitter space), which
    the depth buffer at GROUND_DEPTH holds. Reversed depth: a point h above the plane is at 0.5 + h / far."""
    n = np.asarray(up, np.float64)
    n = n / max(np.linalg.norm(n), 1e-9)
    helper = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 0, 1.0])
    u = np.cross(n, helper)
    u /= np.linalg.norm(u)
    w = np.cross(n, u)
    e, far = GROUND_EXTENT, GROUND_FAR
    m = np.zeros((4, 4))  # clip = [p, 1] @ m
    m[:3, 0], m[:3, 1], m[:3, 2] = u / e, w / e, n / far
    m[3, 2], m[3, 3] = GROUND_DEPTH - height / far, 1.0
    inverse = np.zeros((4, 4))  # [p, 1] = clip @ inverse
    inverse[0, :3], inverse[1, :3], inverse[2, :3] = u * e, w * e, n * far
    inverse[3, :3], inverse[3, 3] = n * (height - GROUND_DEPTH * far), 1.0
    v = _view()
    f = v.view(np.float32)
    f[0:16] = m.astype(np.float32).ravel()
    f[16:32] = inverse.astype(np.float32).ravel()
    f[224:226] = 2.0 / GROUND_TEXELS  # one texel, in clip space
    return v


def volume_texture(header, data, decode_colors, decode_alpha):
    """A BC3 (format 60/61) or RGBA8 3D texture as float (D, H, W, 4) in 0..1."""
    fmt = struct.unpack_from('<I', header, 12)[0]
    width, height, depth = struct.unpack_from('<HHH', header, 22)
    if fmt in (18, 19, 20, 21):
        return np.frombuffer(data[:width * height * depth * 4], np.uint8).reshape(depth, height, width, 4) / 255.0
    if fmt not in (60, 61):
        raise ValueError('Volume texture format %d is not decoded' % fmt)
    bw, bh = max(width // 4, 1), max(height // 4, 1)
    per = bw * bh * 16
    slices = []
    for z in range(depth):
        raw = np.frombuffer(data[z * per:(z + 1) * per], np.uint8).reshape(-1, 16)
        rgb, alpha = decode_colors(raw[:, 8:]), decode_alpha(raw[:, :8])
        rgba = np.concatenate([rgb, alpha[:, :, None]], 2).reshape(bh, bw, 4, 4, 4)
        slices.append(rgba.transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4))
    return np.stack(slices).astype(np.float32) / 255.0
