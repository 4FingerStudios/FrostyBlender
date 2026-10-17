"""Optional "game shaders": Principled materials wired the way GW1/GW2's own shaders read their textures.

Read from the games' compiled GBuffer pixel shaders (GW2's shader database, ps_5_0):
  Normal          X = R * A, Y = G, Z rebuilt (DirectX green, flipped for Blender)
  ASM             R ambient occlusion, G smoothness, B metalness
  ETT             R emissive mask, B tint mask: base = lerp(Color, Color * tint, ETT.B)
  Specular_Masks  R smoothness (x Smoothness_Scale), B tint mask (Tint_Colour); no metal
Prop shaders pick their tint and emissive colours by UV quadrant (u <= 0, v <= 1 -> Q1..Q4);
character shaders use Tint_Color and Emissive_Color. Goop, buffs and blinking lights are not ported.
"""
import bpy

QUADRANT_TINT = ('TintColour_Q1', 'TintColour_Q2', 'TintColour_Q3', 'TintColour_Q4')
QUADRANT_EMISSIVE = ('EmissiveColour_Q1', 'EmissiveColour_Q2', 'EmissiveColour_Q3', 'EmissiveColour_Q4')


def family(doc):
    textures = doc.get('textures', {})
    if textures.get('asm') or textures.get('ett'):
        return 'asm'
    if textures.get('specular_masks'):
        return 'specular_masks'
    return None


class _Nodes:
    def __init__(self, mat):
        self.nodes, self.links = mat.node_tree.nodes, mat.node_tree.links
        self.rows = {}

    def new(self, kind, x, label=None, **settings):
        n = self.nodes.new(kind)
        for key, value in settings.items():
            setattr(n, key, value)
        row = self.rows.get(x, 0)
        self.rows[x] = row + 1
        n.location = (x, -row * 190)
        if label:
            n.label = label
        return n

    def link(self, a, b):
        self.links.new(a, b)

    def set(self, socket, value):
        if isinstance(value, bpy.types.NodeSocket):
            self.link(value, socket)
        elif value is not None:
            socket.default_value = value

    def math(self, op, a, b=None, c=None, x=-250):
        n = self.new('ShaderNodeMath', x, operation=op)
        for i, v in enumerate((a, b, c)):
            self.set(n.inputs[i], v)
        return n.outputs[0]

    def mix(self, a, b, factor, blend='MIX', x=-250):
        n = self.new('ShaderNodeMix', x, data_type='RGBA', blend_type=blend)
        self.set(n.inputs['Factor'], factor)
        self.set(n.inputs['A'], a)
        self.set(n.inputs['B'], b)
        return n.outputs['Result']

    def split(self, tex, x=-600):
        n = self.new('ShaderNodeSeparateColor', x)
        self.link(tex.outputs['Color'], n.inputs['Color'])
        return n


def build(mat, doc, image):
    """Fill `mat`; False (untouched) when its shader family is not one of the decoded ones."""
    kind = family(doc)
    if kind is None:
        return False
    textures, vectors = doc.get('textures', {}), doc.get('vectors', {}) or {}
    params = doc.get('parameters') or [[1, 1, 1, 1], [.7, 0, 1, 0], [1, 1, 0, 0], [0, 0, 0, 0]]
    tiling = params[2]
    cutoff = doc.get('alpha_cutoff', -1)
    cutoff = -1.0 if cutoff is None or cutoff < 0 else float(cutoff)
    mat.use_nodes = True
    mat.use_backface_culling = not doc.get('double_sided', False)
    mat['frostbite_game_shader'] = kind
    mat.node_tree.nodes.clear()
    t = _Nodes(mat)

    def rgba(name, default=(1.0, 1.0, 1.0)):
        v = vectors.get(name)
        return (v[0], v[1], v[2], 1.0) if v else (*default, 1.0)

    uvmap = t.new('ShaderNodeUVMap', -1350)
    tile = t.new('ShaderNodeVectorMath', -1150, operation='MULTIPLY_ADD')
    tile.inputs[1].default_value = (tiling[0], tiling[1], 1)
    tile.inputs[2].default_value = (tiling[2], 1 - tiling[1] - tiling[3], 0)
    t.link(uvmap.outputs['UV'], tile.inputs[0])

    def texture(role, non_color, label):
        path = textures.get(role)
        img = image(path, non_color) if path else None
        if img is None:
            return None
        n = t.new('ShaderNodeTexImage', -900, label=label, image=img)
        t.link(tile.outputs['Vector'], n.inputs['Vector'])
        return n

    base = texture('base', False, 'Color')
    normal = texture('normal', True, 'Normal (X = R x A)')
    if kind == 'asm':
        masks = texture('asm', True, 'ASM (occlusion, smoothness, metal)')
        ett = texture('ett', True, 'ETT (emissive, -, tint mask)')
    else:
        masks = texture('specular_masks', True, 'Specular_Masks (smoothness, -, tint mask)')
        ett = None
    bsdf = t.new('ShaderNodeBsdfPrincipled', 300)
    out = t.new('ShaderNodeOutputMaterial', 650)
    t.link(bsdf.outputs['BSDF'], out.inputs['Surface'])

    def by_quadrant(names, singles):
        """Prop shaders: the colour for this UV quadrant. Others: their single colour."""
        if not any(n in vectors for n in names):
            for single in singles:
                if single in vectors:
                    return rgba(single)
            return (1.0, 1.0, 1.0, 1.0) if names is QUADRANT_TINT else (0.0, 0.0, 0.0, 1.0)
        uv = t.new('ShaderNodeSeparateXYZ', -600)
        t.link(uvmap.outputs['UV'], uv.inputs[0])
        left = t.math('LESS_THAN', uv.outputs['X'], 1e-5)       # u <= 0
        top = t.math('GREATER_THAN', uv.outputs['Y'], -1e-5)     # Frostbite v <= 1 (Blender's V is flipped)
        q1, q2, q3, q4 = (rgba(n) for n in names)
        return t.mix(t.mix(q3, q4, left), t.mix(q1, q2, left), top)

    color = base.outputs['Color'] if base else (1.0, 1.0, 1.0, 1.0)
    sm = t.split(masks) if masks else None
    et = t.split(ett) if ett else None
    tint_mask = et.outputs['Blue'] if et else (sm.outputs['Blue'] if kind == 'specular_masks' and sm else None)
    if tint_mask is None and kind == 'asm' and any(n in vectors for n in QUADRANT_TINT + ('Tint_Color',)):
        tint_mask = 1.0  # shaders without ETT tint the whole surface
    if tint_mask is not None:
        tint = by_quadrant(QUADRANT_TINT, ('Tint_Color', 'Tint_Colour'))
        color = t.mix(color, t.mix(color, tint, 1.0, 'MULTIPLY'), tint_mask)
    if kind == 'asm' and sm:
        color = t.mix(color, sm.outputs['Red'], 1.0, 'MULTIPLY')   # ambient occlusion
        t.link(t.math('SUBTRACT', 1.0, sm.outputs['Green']), bsdf.inputs['Roughness'])
        t.link(sm.outputs['Blue'], bsdf.inputs['Metallic'])
    elif kind == 'specular_masks' and sm:
        scale = (vectors.get('Smoothness_Scale') or [1.0])[0] or 1.0
        t.link(t.math('SUBTRACT', 1.0, t.math('MULTIPLY', sm.outputs['Red'], scale)), bsdf.inputs['Roughness'])
        bsdf.inputs['Metallic'].default_value = 0.0
    else:
        bsdf.inputs['Roughness'].default_value = 0.7
    t.set(bsdf.inputs['Base Color'], color)
    if et:
        emissive = by_quadrant(QUADRANT_EMISSIVE, ('Emissive_Color',))
        t.link(t.mix(emissive, et.outputs['Red'], 1.0, 'MULTIPLY'), bsdf.inputs['Emission Color'])
        bsdf.inputs['Emission Strength'].default_value = 1.0

    if normal:
        sep = t.split(normal)
        x = t.math('MULTIPLY_ADD', t.math('MULTIPLY', sep.outputs['Red'], normal.outputs['Alpha']), 2.0, -1.0)
        y = t.math('MULTIPLY_ADD', sep.outputs['Green'], -2.0, 1.0)
        z = t.math('SQRT', t.math('MAXIMUM', t.math('SUBTRACT', t.math('SUBTRACT', 1.0, t.math('MULTIPLY', x, x)),
                                                    t.math('MULTIPLY', y, y)), 0.0))
        vec = t.new('ShaderNodeCombineXYZ', -100)
        t.link(x, vec.inputs['X'])
        t.link(y, vec.inputs['Y'])
        t.link(z, vec.inputs['Z'])
        pack = t.new('ShaderNodeVectorMath', 0, operation='MULTIPLY_ADD')
        pack.inputs[1].default_value = pack.inputs[2].default_value = (.5, .5, .5)
        t.link(vec.outputs[0], pack.inputs[0])
        nmap = t.new('ShaderNodeNormalMap', 150)
        t.link(pack.outputs[0], nmap.inputs['Color'])
        t.link(nmap.outputs['Normal'], bsdf.inputs['Normal'])

    if base and cutoff >= 0:
        t.link(t.math('GREATER_THAN', base.outputs['Alpha'], cutoff), bsdf.inputs['Alpha'])
    for node in t.nodes:
        node.select = False
    if base:
        base.select = True
        t.nodes.active = base
    return True
