"""Shader Model 5 compute shaders (DXBC) decoded and run on the CPU with numpy, one lane per thread.

Battle for Neighborville's EmitterGraph particles are node graphs compiled into a spawn and a simulate
compute shader; running those shaders is the only faithful way to know what the particles do. Only
what compute shaders use is implemented: structured control flow, float/integer/bit arithmetic,
raw and structured buffers, typed texture sampling (2D/3D, linear, wrap or clamp) and atomics on raw
buffers. Every lane runs every instruction under an execution mask (the GPU's own model), so a
dispatch costs one numpy operation per instruction, not per thread.

    program = parse(bytes)         # the SHEX/SHDR chunk of a DXBC container
    run(program, threads, bindings)
"""
import struct

import numpy as np

# Opcodes (d3d11TokenizedProgramFormat.hpp)
OP = dict(ADD=0, AND=1, BREAK=2, BREAKC=3, CASE=6, CONTINUE=7, CONTINUEC=8, DEFAULT=10, DIV=14, DP2=15, DP3=16,
          DP4=17, ELSE=18, ENDIF=21, ENDLOOP=22, ENDSWITCH=23, EQ=24, EXP=25, FRC=26, FTOI=27, FTOU=28, GE=29,
          IADD=30, IF=31, IEQ=32, IGE=33, ILT=34, IMAD=35, IMAX=36, IMIN=37, IMUL=38, INE=39, INEG=40, ISHL=41,
          ISHR=42, ITOF=43, LD=45, LOG=47, LOOP=48, LT=49, MAD=50, MIN=51, MAX=52, CUSTOMDATA=53, MOV=54,
          MOVC=55, MUL=56, NE=57, NOP=58, NOT=59, OR=60, RESINFO=61, RET=62, RETC=63, ROUND_NE=64, ROUND_NI=65,
          ROUND_PI=66, ROUND_Z=67, RSQ=68, SAMPLE=69, SAMPLE_L=72, SAMPLE_D=73, SAMPLE_B=74, SQRT=75, SWITCH=76,
          SINCOS=77, UDIV=78, ULT=79, UGE=80, UMUL=81, UMAD=82, UMAX=83, UMIN=84, USHR=85, UTOF=86, XOR=87,
          DCL_RESOURCE=88, DCL_CONSTANT_BUFFER=89, DCL_SAMPLER=90, DCL_INPUT=95, DCL_TEMPS=104,
          DCL_INDEXABLE_TEMP=105, DCL_GLOBAL_FLAGS=106, LOD=108, GATHER4=109, BUFINFO=121, RCP=129,
          F32TOF16=130, F16TOF32=131, COUNTBITS=134, FIRSTBIT_HI=135, FIRSTBIT_LO=136, FIRSTBIT_SHI=137,
          UBFE=138, IBFE=139, BFI=140, BFREV=141, DCL_THREAD_GROUP=155, DCL_UAV_TYPED=156, DCL_UAV_RAW=157,
          DCL_UAV_STRUCTURED=158, DCL_TGSM_RAW=159, DCL_TGSM_STRUCTURED=160, DCL_RESOURCE_RAW=161,
          DCL_RESOURCE_STRUCTURED=162, LD_UAV_TYPED=163, STORE_UAV_TYPED=164, LD_RAW=165, STORE_RAW=166,
          LD_STRUCTURED=167, STORE_STRUCTURED=168, ATOMIC_AND=169, ATOMIC_OR=170, ATOMIC_XOR=171,
          ATOMIC_CMP_STORE=172, ATOMIC_IADD=173, ATOMIC_IMAX=174, ATOMIC_IMIN=175, ATOMIC_UMAX=176,
          ATOMIC_UMIN=177, IMM_ATOMIC_ALLOC=178, IMM_ATOMIC_CONSUME=179, IMM_ATOMIC_IADD=180, IMM_ATOMIC_AND=181,
          IMM_ATOMIC_OR=182, IMM_ATOMIC_XOR=183, IMM_ATOMIC_EXCH=184, IMM_ATOMIC_CMP_EXCH=185,
          IMM_ATOMIC_IMAX=186, IMM_ATOMIC_IMIN=187, IMM_ATOMIC_UMAX=188, IMM_ATOMIC_UMIN=189, SYNC=190)
NAME = {v: k for k, v in OP.items()}
DECLARATIONS = {OP[k] for k in OP if k.startswith('DCL_')}

# Operand types
TEMP, INPUT, OUTPUT, INDEXABLE_TEMP, IMM32, IMM64, SAMPLER, RESOURCE, CB, ICB = range(10)
NULL = 13
UAV, TGSM, THREAD_ID, GROUP_ID, THREAD_ID_IN_GROUP, THREAD_ID_FLAT = 30, 31, 32, 33, 34, 36


class Operand:
    __slots__ = ('type', 'mask', 'swizzle', 'index', 'neg', 'abs', 'imm')

    def __repr__(self):
        return 'Operand(%d %s %s)' % (self.type, self.index, self.swizzle or self.mask)


class Instruction:
    __slots__ = ('op', 'operands', 'sat', 'test_nz', 'extra', 'target', 'cases', 'default')

    def __repr__(self):
        return '%s %s' % (NAME.get(self.op, self.op), self.operands)


def container(data):
    """The shader program chunk of a DXBC container."""
    if data[:4] != b'DXBC':
        raise ValueError('Not a DXBC container')
    count = struct.unpack_from('<I', data, 28)[0]
    for i in range(count):
        at = struct.unpack_from('<I', data, 32 + 4 * i)[0]
        if data[at:at + 4] in (b'SHEX', b'SHDR'):
            size = struct.unpack_from('<I', data, at + 4)[0]
            return data[at + 8:at + 8 + size]
    raise ValueError('DXBC has no shader program')


def _operand(tokens, at):
    token = tokens[at]
    at += 1
    o = Operand()
    o.type = (token >> 12) & 0xFF
    o.neg = o.abs = False
    o.mask, o.swizzle, o.imm = 0xF, None, None
    components = token & 3
    if components == 2:
        mode = (token >> 2) & 3
        if mode == 0:
            o.mask = (token >> 4) & 0xF
        elif mode == 1:
            o.swizzle = [(token >> (4 + 2 * k)) & 3 for k in range(4)]
        else:
            o.swizzle = [(token >> 4) & 3] * 4
    elif components == 1:
        o.mask, o.swizzle = 1, [0, 0, 0, 0]
    elif components == 0:
        o.mask = 0
    extended = token >> 31
    while extended:
        ext = tokens[at]
        at += 1
        if ext & 0x3F == 1:
            modifier = (ext >> 6) & 0xFF
            o.neg, o.abs = modifier in (1, 3), modifier in (2, 3)
        extended = ext >> 31
    if o.type in (IMM32, IMM64):
        n = 4 if components == 2 else 1
        if o.type == IMM64:
            raise ValueError('64-bit immediates are not supported')
        o.imm = np.array(tokens[at:at + n] * (4 if n == 1 else 1), np.uint32)
        o.swizzle = [0, 1, 2, 3]
        at += n
        o.index = ()
        return o, at
    dims = (token >> 20) & 3
    index = []
    for d in range(dims):
        rep = (token >> (22 + 3 * d)) & 7
        if rep == 0:
            index.append((tokens[at], None))
            at += 1
        elif rep == 2:
            rel, at = _operand(tokens, at)
            index.append((0, rel))
        elif rep == 3:
            base = tokens[at]
            rel, at = _operand(tokens, at + 1)
            index.append((base, rel))
        else:
            raise ValueError('64-bit operand indices are not supported')
    o.index = tuple(index)
    return o, at


def parse(data):
    """Instructions of a compute shader, with structured control flow matched up."""
    if data[:4] == b'DXBC':
        data = container(data)
    tokens = list(struct.unpack('<%dI' % (len(data) // 4), data))
    version, length = tokens[0], tokens[1]
    if (version >> 16) != 5:  # compute shader
        raise ValueError('Not a compute shader')
    program = {'code': [], 'temps': 0, 'indexable': {}, 'icb': None, 'group': (1, 1, 1), 'resources': {},
               'uavs': {}, 'tgsm': {}}
    at = 2
    end = min(length, len(tokens))
    while at < end:
        token = tokens[at]
        op = token & 0x7FF
        size = (token >> 24) & 0x7F
        if op == OP['CUSTOMDATA']:
            size = tokens[at + 1]
            if (token >> 11) == 3:  # immediate constant buffer
                values = np.array(tokens[at + 2:at + size], np.uint32)
                program['icb'] = values.reshape(-1, 4)
            at += size
            continue
        if size == 0:
            raise ValueError('Malformed instruction token')
        body = tokens[at:at + size]
        i = Instruction()
        i.op, i.sat, i.test_nz = op, bool(token >> 13 & 1), bool(token >> 18 & 1)
        i.extra, i.target, i.cases, i.default = {}, None, None, None
        k = 1
        extended = token >> 31
        while extended:
            ext = body[k]
            k += 1
            if ext & 0x3F == 1:  # sample offsets (aoffimmi)
                i.extra['offset'] = [((ext >> s) & 0xF) - (16 if (ext >> s) & 8 else 0) for s in (9, 13, 17)]
            extended = ext >> 31
        if op in DECLARATIONS:
            if op == OP['DCL_TEMPS']:
                program['temps'] = body[1]
            elif op == OP['DCL_INDEXABLE_TEMP']:
                program['indexable'][body[1]] = (body[2], body[3])
            elif op == OP['DCL_THREAD_GROUP']:
                program['group'] = tuple(body[1:4])
            elif op == OP['DCL_RESOURCE']:
                o, _ = _operand(body, 1)
                program['resources'][o.index[0][0]] = ('texture', (token >> 11) & 0x1F)
            elif op == OP['DCL_RESOURCE_STRUCTURED']:
                o, n = _operand(body, 1)
                program['resources'][o.index[0][0]] = ('structured', body[n])
            elif op == OP['DCL_RESOURCE_RAW']:
                o, _ = _operand(body, 1)
                program['resources'][o.index[0][0]] = ('raw', 0)
            elif op in (OP['DCL_UAV_RAW'], OP['DCL_UAV_STRUCTURED'], OP['DCL_UAV_TYPED']):
                o, n = _operand(body, 1)
                program['uavs'][o.index[0][0]] = ('structured', body[n]) if op == OP['DCL_UAV_STRUCTURED'] else ('raw', 0)
            elif op in (OP['DCL_TGSM_RAW'], OP['DCL_TGSM_STRUCTURED']):
                o, n = _operand(body, 1)
                stride, count = (4, body[n] // 4) if op == OP['DCL_TGSM_RAW'] else (body[n], body[n + 1])
                program['tgsm'][o.index[0][0]] = (stride, count)
            at += size
            continue
        operands = []
        while k < size:
            o, k = _operand(body, k)
            operands.append(o)
        i.operands = operands
        program['code'].append(i)
        at += size
    _match(program['code'])
    return program


def _match(code):
    stack = []
    for n, i in enumerate(code):
        if i.op in (OP['IF'], OP['LOOP'], OP['SWITCH']):
            stack.append(n)
            if i.op == OP['SWITCH']:
                i.cases, i.default = [], None
        elif i.op == OP['ELSE']:
            code[stack[-1]].target = n
        elif i.op in (OP['ENDIF'], OP['ENDLOOP'], OP['ENDSWITCH']):
            start = stack.pop()
            i.target = start
            if code[start].op == OP['IF']:
                if code[start].target is not None:
                    code[code[start].target].target = n
                else:
                    code[start].target = n
            else:
                code[start].target = n
        elif i.op == OP['CASE']:
            code[stack[-1]].cases.append((int(i.operands[0].imm[0]), n))
        elif i.op == OP['DEFAULT']:
            code[stack[-1]].default = n
    if stack:
        raise ValueError('Unbalanced shader control flow')


# ---------------- execution ----------------
F, U, I = np.float32, np.uint32, np.int32


def _f(x):
    return x.view(F)


def _i(x):
    return x.view(I)


class Texture:
    """A sampled texture: float32 (H, W, 4) or (D, H, W, 4); wrap addressing unless clamp is set."""

    def __init__(self, pixels, clamp=False):
        self.pixels = np.asarray(pixels, F)
        self.clamp = clamp

    def sample(self, uvw, clamp=None):
        clamp = self.clamp if clamp is None else clamp
        p = self.pixels
        dims = p.ndim - 1
        size = np.array(p.shape[:dims][::-1], F)  # (W, H[, D]) in uvw order
        coords = uvw[:, :dims] * size - 0.5
        base = np.floor(coords)
        frac = coords - base
        base = base.astype(np.int64)
        out = np.zeros((len(uvw), 4), F)
        for corner in range(1 << dims):
            weight = np.ones(len(uvw), F)
            idx = []
            for d in range(dims):
                bit = (corner >> d) & 1
                weight = weight * (frac[:, d] if bit else 1 - frac[:, d])
                c = base[:, d] + bit
                n = p.shape[dims - 1 - d]
                idx.append(np.clip(c, 0, n - 1) if clamp else np.mod(c, n))
            texel = p[tuple(idx[::-1])]
            out += texel * weight[:, None]
        return out

    def gather(self, uv, channel):
        p = self.pixels
        h, w = p.shape[:2]
        x, y = uv[:, 0] * w - 0.5, uv[:, 1] * h - 0.5
        x0, y0 = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
        def at(xx, yy):
            return p[np.mod(yy, h), np.mod(xx, w), channel]
        # gather order: (0,1), (1,1), (1,0), (0,0)
        return np.stack([at(x0, y0 + 1), at(x0 + 1, y0 + 1), at(x0 + 1, y0), at(x0, y0)], 1).astype(F)


class Machine:
    def __init__(self, program, n, bindings):
        self.p, self.n, self.b = program, n, bindings
        self.r = np.zeros((max(program['temps'], 1), n, 4), U)
        self.x = {k: np.zeros((size, n, 4), U) for k, (size, _) in program['indexable'].items()}
        self.lanes = np.arange(n)

    # ---- operand access
    def index_value(self, entry):
        base, rel = entry
        if rel is None:
            return base
        return base + _i(self.read(rel))[:, 0].astype(np.int64)

    def raw(self, o):
        t = o.type
        if t == IMM32:
            return np.broadcast_to(o.imm, (self.n, 4))
        if t == TEMP:
            return self.r[o.index[0][0]]
        if t == INDEXABLE_TEMP:
            reg, slot = o.index[0][0], self.index_value(o.index[1])
            store = self.x[reg]
            return store[slot] if np.isscalar(slot) else store[slot, self.lanes]
        if t == CB:
            buffer = self.b['cb'][o.index[0][0]]
            row = self.index_value(o.index[1])
            return np.broadcast_to(buffer[row], (self.n, 4)) if np.isscalar(row) else buffer[np.clip(row, 0, len(buffer) - 1)]
        if t == ICB:
            row = self.index_value(o.index[0])
            icb = self.p['icb']
            return np.broadcast_to(icb[row], (self.n, 4)) if np.isscalar(row) else icb[np.clip(row, 0, len(icb) - 1)]
        if t in (THREAD_ID, THREAD_ID_FLAT, THREAD_ID_IN_GROUP, GROUP_ID):
            return self.b['thread_ids'][t]
        raise ValueError('Unsupported source operand type %d' % t)

    def read(self, o, kind=None):
        v = self.raw(o)
        if o.swizzle is not None:
            v = v[:, o.swizzle]
        if o.abs or o.neg:
            if kind == 'i':
                v = _i(v.copy())
                if o.abs:
                    v = np.abs(v)
                if o.neg:
                    v = -v
                v = v.view(U)
            else:
                v = _f(v.copy())
                if o.abs:
                    v = np.abs(v)
                if o.neg:
                    v = -v
                v = v.view(U)
        return v

    def readf(self, o):
        return _f(np.ascontiguousarray(self.read(o, 'f')))

    def readi(self, o):
        return _i(np.ascontiguousarray(self.read(o, 'i')))

    def readu(self, o):
        return np.ascontiguousarray(self.read(o, 'i'))

    def write(self, o, value, active, sat=False):
        if o.type == NULL:
            return
        value = np.asarray(value)
        if value.dtype == F:
            if sat:
                value = np.clip(np.nan_to_num(value, nan=0.0), 0, 1).astype(F)
            value = value.view(U)
        elif value.dtype != U:
            value = value.astype(I).view(U) if value.dtype.kind == 'i' else value.astype(U)
        if value.ndim == 1:
            value = value[:, None]
        if value.shape[1] == 1:
            value = np.repeat(value, 4, 1)
        if o.type == TEMP:
            target = self.r[o.index[0][0]]
        elif o.type == INDEXABLE_TEMP:
            reg, slot = o.index[0][0], self.index_value(o.index[1])
            if not np.isscalar(slot):
                store = self.x[reg]
                for c in range(4):
                    if o.mask >> c & 1:
                        rows = slot[active]
                        store[rows, self.lanes[active], c] = value[active, c]
                return
            target = self.x[reg][slot]
        else:
            raise ValueError('Unsupported destination operand type %d' % o.type)
        for c in range(4):
            if o.mask >> c & 1:
                target[active, c] = value[active, c]


def _components(mask):
    return [c for c in range(4) if mask >> c & 1]


def _load(buffer, address, count):
    """count dwords from byte addresses (n,) of a uint32 buffer."""
    words = (address // 4).astype(np.int64)
    idx = words[:, None] + np.arange(count)
    ok = (idx >= 0) & (idx < len(buffer))
    return np.where(ok, buffer[np.clip(idx, 0, len(buffer) - 1)], 0).astype(U)


def _store(buffer, address, values, active):
    words = (address[active] // 4).astype(np.int64)
    for c in range(values.shape[1]):
        w = words + c
        ok = (w >= 0) & (w < len(buffer))
        buffer[w[ok]] = values[active][ok, c]


def run(program, threads, bindings, limit=2_000_000):
    """Run `threads` lanes of `program`. bindings: cb {slot: (rows, 4) uint32}, buffers {t slot: (uint32 array,
    stride)}, uavs {u slot: uint32 array}, textures {t slot: Texture}, thread_id offset (optional)."""
    n = int(threads)
    if n <= 0:
        return
    tid = np.arange(n, dtype=U) + U(bindings.get('first_thread', 0))
    gx = program['group'][0] or 1
    ids = {THREAD_ID: np.stack([tid, np.zeros(n, U), np.zeros(n, U), np.zeros(n, U)], 1),
           THREAD_ID_FLAT: np.repeat((tid % gx)[:, None], 4, 1),
           THREAD_ID_IN_GROUP: np.stack([tid % gx, np.zeros(n, U), np.zeros(n, U), np.zeros(n, U)], 1),
           GROUP_ID: np.stack([tid // gx, np.zeros(n, U), np.zeros(n, U), np.zeros(n, U)], 1)}
    b = dict(bindings, thread_ids=ids)
    b.setdefault('cb', {})
    b.setdefault('buffers', {})
    b.setdefault('uavs', {})
    b.setdefault('textures', {})
    tgsm = {k: np.zeros(stride * count // 4 + 4, U) for k, (stride, count) in program['tgsm'].items()}
    m = Machine(program, n, b)
    code = program['code']
    active = np.ones(n, bool)
    returned = np.zeros(n, bool)
    frames = []  # [kind, saved, extra...]
    pc, steps = 0, 0

    def removed():
        """Lanes taken out of the current block by break/continue/return."""
        out = returned.copy()
        for f in reversed(frames):
            if f[0] in ('loop', 'switch'):
                out |= f[2]
                if f[0] == 'loop':
                    out |= f[3]
                break
        return out

    while pc < len(code):
        steps += 1
        if steps > limit:
            raise ValueError('Shader ran too long')
        ins = code[pc]
        op, ops = ins.op, ins.operands
        pc += 1
        # ---- control flow
        if op == OP['IF']:
            cond = m.readu(ops[0])[:, 0] != 0
            if not ins.test_nz:
                cond = ~cond
            frames.append(['if', active.copy(), cond])
            active = active & cond
            if not active.any():
                pc = ins.target  # to else or endif
                if code[pc].op == OP['ELSE']:
                    active = frames[-1][1] & ~cond & ~removed()
                    pc += 1
            continue
        if op == OP['ELSE']:
            f = frames[-1]
            active = f[1] & ~f[2] & ~removed()
            if not active.any():
                pc = ins.target
            continue
        if op == OP['ENDIF']:
            f = frames.pop()
            active = f[1] & ~removed()
            continue
        if op == OP['LOOP']:
            frames.append(['loop', active.copy(), np.zeros(n, bool), np.zeros(n, bool), pc])
            continue
        if op == OP['ENDLOOP']:
            f = frames[-1]
            f[3][:] = False
            active = f[1] & ~f[2] & ~returned
            if active.any():
                pc = f[4]
            else:
                frames.pop()
                active = f[1] & ~returned
                active &= ~removed()
            continue
        if op in (OP['BREAK'], OP['BREAKC'], OP['CONTINUE'], OP['CONTINUEC']):
            if op in (OP['BREAKC'], OP['CONTINUEC']):
                cond = m.readu(ops[0])[:, 0] != 0
                if not ins.test_nz:
                    cond = ~cond
                hit = active & cond
            else:
                hit = active.copy()
            f = next(f for f in reversed(frames) if f[0] in ('loop', 'switch'))
            if op in (OP['BREAK'], OP['BREAKC']):
                f[2] |= hit
            else:
                f = next(f for f in reversed(frames) if f[0] == 'loop')
                f[3] |= hit
            active = active & ~hit
            continue
        if op == OP['SWITCH']:
            value = m.readi(ops[0])[:, 0]
            frames.append(['switch', active.copy(), np.zeros(n, bool), value, np.zeros(n, bool), ins])
            active = np.zeros(n, bool)
            continue
        if op == OP['CASE']:
            f = frames[-1]
            f[4] |= f[1] & (f[3] == int(_i(ins.operands[0].imm)[0]))
            active = f[4] & ~f[2] & ~returned
            continue
        if op == OP['DEFAULT']:
            f = frames[-1]
            values = [v for v, _ in f[5].cases]
            f[4] |= f[1] & ~np.isin(f[3], values)
            active = f[4] & ~f[2] & ~returned
            continue
        if op == OP['ENDSWITCH']:
            f = frames.pop()
            active = f[1] & ~returned
            active &= ~removed()
            continue
        if op in (OP['RET'], OP['RETC']):
            if op == OP['RETC']:
                cond = m.readu(ops[0])[:, 0] != 0
                if not ins.test_nz:
                    cond = ~cond
                hit = active & cond
            else:
                hit = active.copy()
            returned |= hit
            active = active & ~hit
            if not frames and not active.any():
                break
            continue
        if op in (OP['NOP'], OP['SYNC']):
            continue
        if not active.any():
            continue
        _execute(m, ins, active, tgsm)


def _cmp(result):
    return np.where(result, U(0xFFFFFFFF), U(0))


def _execute(m, ins, active, tgsm):
    op, ops, sat = ins.op, ins.operands, ins.sat
    d = ops[0] if ops else None
    if op == OP['MOV']:
        v = m.read(ops[1], 'f')
        m.write(d, _f(np.ascontiguousarray(v)) if sat else v, active, sat)
    elif op == OP['MOVC']:
        c = m.readu(ops[1]) != 0
        m.write(d, np.where(c, m.read(ops[2], 'f'), m.read(ops[3], 'f')), active, False) if not sat else \
            m.write(d, _f(np.ascontiguousarray(np.where(c, m.read(ops[2], 'f'), m.read(ops[3], 'f')))), active, True)
    elif op in (OP['ADD'], OP['MUL'], OP['DIV'], OP['MIN'], OP['MAX']):
        a, c = m.readf(ops[1]), m.readf(ops[2])
        with np.errstate(all='ignore'):
            v = {OP['ADD']: np.add, OP['MUL']: np.multiply, OP['DIV']: np.divide,
                 OP['MIN']: np.fmin, OP['MAX']: np.fmax}[op](a, c).astype(F)
        m.write(d, v, active, sat)
    elif op == OP['MAD']:
        with np.errstate(all='ignore'):
            m.write(d, (m.readf(ops[1]) * m.readf(ops[2]) + m.readf(ops[3])).astype(F), active, sat)
    elif op in (OP['DP2'], OP['DP3'], OP['DP4']):
        k = {OP['DP2']: 2, OP['DP3']: 3, OP['DP4']: 4}[op]
        with np.errstate(all='ignore'):
            v = (m.readf(ops[1])[:, :k] * m.readf(ops[2])[:, :k]).sum(1).astype(F)
        m.write(d, v, active, sat)
    elif op in (OP['FRC'], OP['RSQ'], OP['SQRT'], OP['EXP'], OP['LOG'], OP['RCP'], OP['ROUND_NE'], OP['ROUND_NI'],
                OP['ROUND_PI'], OP['ROUND_Z']):
        a = m.readf(ops[1])
        with np.errstate(all='ignore'):
            if op == OP['FRC']:
                v = a - np.floor(a)
            elif op == OP['RSQ']:
                v = 1 / np.sqrt(a)
            elif op == OP['SQRT']:
                v = np.sqrt(a)
            elif op == OP['EXP']:
                v = np.exp2(a)
            elif op == OP['LOG']:
                v = np.log2(a)
            elif op == OP['RCP']:
                v = 1 / a
            else:
                v = {OP['ROUND_NE']: np.rint, OP['ROUND_NI']: np.floor, OP['ROUND_PI']: np.ceil,
                     OP['ROUND_Z']: np.trunc}[op](a)
        m.write(d, v.astype(F), active, sat)
    elif op == OP['SINCOS']:
        a = m.readf(ops[2])
        with np.errstate(all='ignore'):
            if ops[0].type != NULL:
                m.write(ops[0], np.sin(a).astype(F), active, sat)
            if ops[1].type != NULL:
                m.write(ops[1], np.cos(a).astype(F), active, sat)
    elif op in (OP['EQ'], OP['NE'], OP['LT'], OP['GE']):
        a, c = m.readf(ops[1]), m.readf(ops[2])
        with np.errstate(invalid='ignore'):
            r = {OP['EQ']: np.equal, OP['NE']: np.not_equal, OP['LT']: np.less, OP['GE']: np.greater_equal}[op](a, c)
        m.write(d, _cmp(r), active)
    elif op in (OP['IEQ'], OP['INE'], OP['ILT'], OP['IGE']):
        a, c = m.readi(ops[1]), m.readi(ops[2])
        r = {OP['IEQ']: np.equal, OP['INE']: np.not_equal, OP['ILT']: np.less, OP['IGE']: np.greater_equal}[op](a, c)
        m.write(d, _cmp(r), active)
    elif op in (OP['ULT'], OP['UGE']):
        a, c = m.readu(ops[1]), m.readu(ops[2])
        m.write(d, _cmp(a < c if op == OP['ULT'] else a >= c), active)
    elif op == OP['IADD']:
        m.write(d, (m.readi(ops[1]).astype(np.int64) + m.readi(ops[2])).astype(np.int64).astype(U), active)
    elif op == OP['IMAD'] or op == OP['UMAD']:
        v = (m.readu(ops[1]).astype(np.int64) * m.readu(ops[2]) + m.readu(ops[3])) & 0xFFFFFFFF
        m.write(d, v.astype(U), active)
    elif op in (OP['IMUL'], OP['UMUL']):
        a, c = m.readu(ops[2]).astype(np.uint64), m.readu(ops[3]).astype(np.uint64)
        full = a * c
        if ops[0].type != NULL:
            m.write(ops[0], (full >> np.uint64(32)).astype(U), active)
        if ops[1].type != NULL:
            m.write(ops[1], (full & np.uint64(0xFFFFFFFF)).astype(U), active)
    elif op == OP['UDIV']:
        a, c = m.readu(ops[2]), m.readu(ops[3])
        safe = np.where(c == 0, 1, c)
        if ops[0].type != NULL:
            m.write(ops[0], np.where(c == 0, U(0xFFFFFFFF), a // safe).astype(U), active)
        if ops[1].type != NULL:
            m.write(ops[1], np.where(c == 0, U(0xFFFFFFFF), a % safe).astype(U), active)
    elif op in (OP['IMAX'], OP['IMIN']):
        a, c = m.readi(ops[1]), m.readi(ops[2])
        m.write(d, (np.maximum if op == OP['IMAX'] else np.minimum)(a, c).astype(I), active)
    elif op in (OP['UMAX'], OP['UMIN']):
        a, c = m.readu(ops[1]), m.readu(ops[2])
        m.write(d, (np.maximum if op == OP['UMAX'] else np.minimum)(a, c).astype(U), active)
    elif op == OP['INEG']:
        m.write(d, (-m.readi(ops[1]).astype(np.int64)).astype(np.int64).astype(U), active)
    elif op in (OP['AND'], OP['OR'], OP['XOR']):
        a, c = m.readu(ops[1]), m.readu(ops[2])
        m.write(d, {OP['AND']: np.bitwise_and, OP['OR']: np.bitwise_or, OP['XOR']: np.bitwise_xor}[op](a, c), active)
    elif op == OP['NOT']:
        m.write(d, ~m.readu(ops[1]), active)
    elif op in (OP['ISHL'], OP['USHR'], OP['ISHR']):
        a, s = m.readu(ops[1]), m.readu(ops[2]) & U(31)
        if op == OP['ISHL']:
            v = (a.astype(np.uint64) << s.astype(np.uint64)) & np.uint64(0xFFFFFFFF)
        elif op == OP['USHR']:
            v = a >> s
        else:
            v = _i(a) >> s.astype(I)
        m.write(d, np.asarray(v).astype(np.int64).astype(U) if op == OP['ISHR'] else np.asarray(v).astype(U), active)
    elif op in (OP['ITOF'], OP['UTOF']):
        v = m.readi(ops[1]) if op == OP['ITOF'] else m.readu(ops[1])
        m.write(d, v.astype(F), active, sat)
    elif op in (OP['FTOI'], OP['FTOU']):
        a = m.readf(ops[1])
        with np.errstate(invalid='ignore'):
            a = np.nan_to_num(np.trunc(a), nan=0.0)
            v = np.clip(a, -2 ** 31, 2 ** 31 - 1).astype(np.int64) if op == OP['FTOI'] else np.clip(a, 0, 2 ** 32 - 1).astype(np.int64)
        m.write(d, v.astype(U) if op == OP['FTOU'] else v.astype(I), active)
    elif op == OP['F32TOF16']:
        v = m.readf(ops[1]).astype(np.float16).view(np.uint16).astype(U)
        m.write(d, v, active)
    elif op == OP['F16TOF32']:
        v = (m.readu(ops[1]) & U(0xFFFF)).astype(np.uint16).view(np.float16).astype(F)
        m.write(d, v, active)
    elif op in (OP['UBFE'], OP['IBFE']):
        width, offset, value = m.readu(ops[1]) & U(31), m.readu(ops[2]) & U(31), m.readu(ops[3])
        w64, o64 = width.astype(np.int64), offset.astype(np.int64)
        v = value.astype(np.int64)
        out = (v >> o64) & ((1 << w64) - 1)
        if op == OP['IBFE']:
            sign = (out >> np.maximum(w64 - 1, 0)) & 1
            out = np.where((sign == 1) & (w64 > 0), out - (1 << w64), out)
        out = np.where(w64 == 0, 0, out)
        m.write(d, out.astype(np.int64).astype(U), active)
    elif op == OP['BFI']:
        width, offset = m.readu(ops[1]) & U(31), m.readu(ops[2]) & U(31)
        insert, base = m.readu(ops[3]).astype(np.int64), m.readu(ops[4]).astype(np.int64)
        mask = (((1 << width.astype(np.int64)) - 1) << offset.astype(np.int64)) & 0xFFFFFFFF
        v = ((insert << offset.astype(np.int64)) & mask) | (base & ~mask & 0xFFFFFFFF)
        m.write(d, v.astype(U), active)
    elif op == OP['COUNTBITS']:
        a = m.readu(ops[1])
        m.write(d, np.unpackbits(a.view(np.uint8).reshape(a.shape + (4,)), axis=-1).sum(-1).astype(U), active)
    elif op in (OP['FIRSTBIT_LO'], OP['FIRSTBIT_HI'], OP['FIRSTBIT_SHI']):
        a = m.readu(ops[1]).astype(np.int64)
        if op == OP['FIRSTBIT_SHI']:
            a = np.where(a & 0x80000000, ~a & 0xFFFFFFFF, a)
        out = np.full(a.shape, 0xFFFFFFFF, np.int64)
        bits = range(32) if op == OP['FIRSTBIT_LO'] else range(31, -1, -1)
        for k in bits:
            hit = (out == 0xFFFFFFFF) & ((a >> k) & 1 == 1)
            out = np.where(hit, k if op == OP['FIRSTBIT_LO'] else 31 - k, out)
        m.write(d, out.astype(U), active)
    elif op == OP['BFREV']:
        a = m.readu(ops[1])
        v = np.zeros_like(a)
        for k in range(32):
            v |= ((a >> U(k)) & U(1)) << U(31 - k)
        m.write(d, v, active)
    elif op in (OP['LD_STRUCTURED'], OP['LD_RAW']):
        src = ops[-1]
        if op == OP['LD_STRUCTURED']:
            index, offset = m.readu(ops[1])[:, 0], m.readu(ops[2])[:, 0]
        else:
            index, offset = np.zeros(m.n, U), m.readu(ops[1])[:, 0]
        if src.type == TGSM:
            buffer, stride = tgsm[src.index[0][0]], m.p['tgsm'][src.index[0][0]][0]
        elif src.type == RESOURCE:
            buffer, stride = m.b['buffers'][src.index[0][0]]
        else:
            buffer = m.b['uavs'][src.index[0][0]]
            stride = m.p['uavs'].get(src.index[0][0], ('raw', 0))[1]
        address = index.astype(np.int64) * stride + offset
        words = _load(buffer, address, 4)
        v = words[:, src.swizzle or [0, 1, 2, 3]]
        m.write(d, v, active)
    elif op in (OP['STORE_STRUCTURED'], OP['STORE_RAW']):
        if op == OP['STORE_STRUCTURED']:
            index, offset, value = m.readu(ops[1])[:, 0], m.readu(ops[2])[:, 0], m.readu(ops[3])
        else:
            index, offset, value = np.zeros(m.n, U), m.readu(ops[1])[:, 0], m.readu(ops[2])
        if d.type == TGSM:
            buffer, stride = tgsm[d.index[0][0]], m.p['tgsm'][d.index[0][0]][0]
        else:
            buffer = m.b['uavs'][d.index[0][0]]
            stride = m.p['uavs'].get(d.index[0][0], ('raw', 0))[1]
        comps = _components(d.mask)
        address = index.astype(np.int64) * stride + offset
        # Components are written to consecutive dwords in mask order from the source's x, y, z, w.
        _store(buffer, address, value[:, :len(comps)], active)
    elif op in (OP['SAMPLE_L'], OP['SAMPLE'], OP['SAMPLE_B'], OP['SAMPLE_D'], OP['LD']):
        coords, res = m.readf(ops[1]), ops[2]
        tex = m.b['textures'].get(res.index[0][0])
        if tex is None:
            v = np.zeros((m.n, 4), F)
        elif op == OP['LD']:
            texel = m.readi(ops[1])
            p = tex.pixels
            dims = p.ndim - 1
            idx = tuple(np.clip(texel[:, dims - 1 - k], 0, p.shape[k] - 1) for k in range(dims))
            v = p[idx]
        else:
            v = tex.sample(coords)
        v = np.asarray(v, F)[:, res.swizzle or [0, 1, 2, 3]]
        m.write(d, v, active, sat)
    elif op == OP['GATHER4']:
        coords, res, sampler = m.readf(ops[1]), ops[2], ops[3]
        tex = m.b['textures'].get(res.index[0][0])
        channel = (sampler.swizzle or [0])[0]
        v = np.ones((m.n, 4), F) if tex is None else tex.gather(coords, channel)
        m.write(d, v[:, res.swizzle or [0, 1, 2, 3]], active)
    elif op == OP['RESINFO'] or op == OP['BUFINFO']:
        res = ops[2] if op == OP['RESINFO'] else ops[1]
        tex = m.b['textures'].get(res.index[0][0])
        dims = list(tex.pixels.shape[:-1][::-1]) if tex is not None else [1, 1]
        v = np.tile(np.array((dims + [1, 1, 1])[:3] + [1], F), (m.n, 1))
        m.write(d, v[:, res.swizzle or [0, 1, 2, 3]], active)
    elif op in (OP['ATOMIC_IADD'], OP['IMM_ATOMIC_IADD'], OP['ATOMIC_UMAX'], OP['ATOMIC_UMIN'], OP['IMM_ATOMIC_UMAX'],
                OP['IMM_ATOMIC_UMIN'], OP['ATOMIC_IMAX'], OP['ATOMIC_IMIN'], OP['IMM_ATOMIC_IMAX'],
                OP['IMM_ATOMIC_IMIN'], OP['ATOMIC_OR'], OP['ATOMIC_AND'], OP['IMM_ATOMIC_OR'], OP['IMM_ATOMIC_AND'],
                OP['IMM_ATOMIC_EXCH'], OP['ATOMIC_XOR'], OP['IMM_ATOMIC_XOR']):
        imm = op >= OP['IMM_ATOMIC_ALLOC']
        dst = ops[1] if imm else ops[0]
        address, value = m.readu(ops[2] if imm else ops[1])[:, 0], m.readu(ops[3] if imm else ops[2])[:, 0]
        buffer = tgsm[dst.index[0][0]] if dst.type == TGSM else m.b['uavs'][dst.index[0][0]]
        stride = m.p['tgsm'][dst.index[0][0]][0] if dst.type == TGSM else m.p['uavs'].get(dst.index[0][0], ('raw', 0))[1]
        if stride:  # structured: (index, offset) packed in the address operand's x and y
            pair = m.readu(ops[2] if imm else ops[1])
            address = pair[:, 0].astype(np.int64) * stride + pair[:, 1]
        old = np.zeros(m.n, U)
        name = NAME[op].replace('IMM_', '')
        for lane in np.nonzero(active)[0]:  # in lane order, as one thread after another
            w = int(address[lane]) // 4
            if not 0 <= w < len(buffer):
                continue
            cur = buffer[w]
            old[lane] = cur
            val = value[lane]
            if name == 'ATOMIC_IADD':
                buffer[w] = U((int(cur) + int(val)) & 0xFFFFFFFF)
            elif name == 'ATOMIC_UMAX':
                buffer[w] = max(cur, val)
            elif name == 'ATOMIC_UMIN':
                buffer[w] = min(cur, val)
            elif name in ('ATOMIC_IMAX', 'ATOMIC_IMIN'):
                a, c = int(np.array(cur).view(I)), int(np.array(val).view(I))
                buffer[w] = U((max(a, c) if name == 'ATOMIC_IMAX' else min(a, c)) & 0xFFFFFFFF)
            elif name == 'ATOMIC_OR':
                buffer[w] = cur | val
            elif name == 'ATOMIC_AND':
                buffer[w] = cur & val
            elif name == 'ATOMIC_XOR':
                buffer[w] = cur ^ val
            elif name == 'ATOMIC_EXCH':
                buffer[w] = val
        if imm:
            m.write(ops[0], old, active)
    else:
        raise ValueError('Unsupported shader instruction %s' % NAME.get(op, op))
