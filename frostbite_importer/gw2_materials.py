"""Garden Warfare 2 materials around one shared node group, "PVZ GW2 Generic".

The group takes the game's textures as they are and does what GW2's GBuffer shaders do with them:
  Normal   X = R * A, Y = G (DirectX green, flipped for Blender), Z rebuilt
  ASM      R ambient occlusion, G smoothness, B metalness
  ETT      R emissive mask, B tint mask: colour = lerp(Color, Color * Tint, ETT.B)
  Edge     velvet/fresnel shaders: Edge_Tint * (1 - N.V) ^ Edge_Tint_Exponent, added to colour and emission
  Detail   greyscale overlay, neutral at 0.5, tiled by Detail_UV_Tiling
Every GW2 material uses the same group, so EEVEE compiles one shader for all of them, and each material's
values sit on the group node where they can be changed. Specular_Masks shaders (R smoothness, B tint mask)
are repacked into ASM/ETT outside the group. Prop shaders pick tint and emissive colours by UV quadrant.
"""
import bpy

from .materials import placeholder

GROUP = 'PVZ GW2 Generic'
VERSION = 1
QUADRANT_TINT = ('TintColour_Q1', 'TintColour_Q2', 'TintColour_Q3', 'TintColour_Q4')
QUADRANT_EMISSIVE = ('EmissiveColour_Q1', 'EmissiveColour_Q2', 'EmissiveColour_Q3', 'EmissiveColour_Q4')

INPUTS = [
    ('Base Colour', 'NodeSocketColor', (1, 1, 1, 1)),
    ('Alpha', 'NodeSocketFloat', 1.0),
    ('Tint Colour', 'NodeSocketColor', (1, 1, 1, 1)),
    ('ETT', 'NodeSocketColor', (0, 1, 1, 1)),           # no emission, whole surface tintable
    ('ASM', 'NodeSocketColor', (1, .3, 0, 1)),          # no occlusion, smoothness 0.3, not metal
    ('Normal', 'NodeSocketColor', (.5, .5, 1, 1)),
    ('Normal Alpha', 'NodeSocketFloat', 1.0),
    ('Edge Tint Colour', 'NodeSocketColor', (0, 0, 0, 1)),
    ('Edge Exponent', 'NodeSocketFloat', 4.0),
    ('Edge Emission Strength', 'NodeSocketFloat', 0.5),
    ('Detail Map', 'NodeSocketColor', (.5, .5, .5, 1)),
    ('Detail Strength', 'NodeSocketFloat', 1.0),
    ('Emission Colour', 'NodeSocketColor', (0, 0, 0, 1)),
    ('Emission Strength', 'NodeSocketFloat', 1.0),
    ('Emission Mode', 'NodeSocketInt', 0),              # 0: ETT red masks the emission, 1: whole surface
    ('Colour AO Strength', 'NodeSocketFloat', 1.0),
    ('Reflection AO Strength', 'NodeSocketFloat', 1.0),
    ('Alpha Cutoff', 'NodeSocketFloat', -1.0),          # below 0: opaque
]
TIPS = {
    'ETT': 'R emissive mask, B tint mask',
    'ASM': 'R ambient occlusion, G smoothness, B metalness',
    'Normal': 'Game normal map: X from R x Normal Alpha, Y from G',
    'Edge Tint Colour': 'Rim colour at grazing angles (velvet and fresnel shaders)',
    'Emission Mode': '0: ETT red masks the emission. 1: the whole surface glows',
    'Alpha Cutoff': 'Alpha test threshold. Below 0 the surface is opaque',
}


class _Builder:
    """Small helper for laying out and linking nodes in a tree, column by column."""

    def __init__(self, tree):
        self.nodes, self.links = tree.nodes, tree.links
        self.rows = {}

    def new(self, kind, x, label=None, **settings):
        n = self.nodes.new(kind)
        for key, value in settings.items():
            setattr(n, key, value)
        row = self.rows.get(x, 0)
        self.rows[x] = row + 1
        n.location = (x, -row * 180)
        if label:
            n.label = label
        return n

    def set(self, socket, value):
        if isinstance(value, bpy.types.NodeSocket):
            self.links.new(value, socket)
        elif value is not None:
            socket.default_value = value

    def math(self, op, a, b=None, c=None, x=0, clamp=False):
        n = self.new('ShaderNodeMath', x, operation=op, use_clamp=clamp)
        for i, v in enumerate((a, b, c)):
            self.set(n.inputs[i], v)
        return n.outputs[0]

    def mix(self, a, b, factor, blend='MIX', x=0, clamp=False):
        n = self.new('ShaderNodeMix', x, data_type='RGBA', blend_type=blend, clamp_result=clamp)
        self.set(n.inputs['Factor'], factor)
        self.set(n.inputs['A'], a)
        self.set(n.inputs['B'], b)
        return n.outputs['Result']

    def vec(self, op, a, b=None, x=0):
        n = self.new('ShaderNodeVectorMath', x, operation=op)
        self.set(n.inputs[0], a)
        if b is not None:
            self.set(n.inputs[1 if op != 'SCALE' else 3], b)
        return n.outputs['Value' if op == 'DOT_PRODUCT' else 'Vector']

    def split(self, colour, x=0):
        n = self.new('ShaderNodeSeparateColor', x)
        self.set(n.inputs['Color'], colour)
        return n.outputs


def group():
    found = bpy.data.node_groups.get(GROUP)
    # Only reuse our own build of this version; a hand-made group with the same name is left alone.
    if found and found.get('frostbite_version') == VERSION:
        return found
    tree = bpy.data.node_groups.new(GROUP, 'ShaderNodeTree')
    tree['frostbite_version'] = VERSION
    for name, kind, default in INPUTS:
        socket = tree.interface.new_socket(name, in_out='INPUT', socket_type=kind)
        socket.default_value = default
        if name in TIPS:
            socket.description = TIPS[name]
        if name == 'Emission Mode':
            socket.min_value, socket.max_value = 0, 1
    tree.interface.new_socket('Shader', in_out='OUTPUT', socket_type='NodeSocketShader')
    tree.interface.new_socket('Edge Tint Preview', in_out='OUTPUT', socket_type='NodeSocketColor')
    t = _Builder(tree)
    gi = t.new('NodeGroupInput', -1600).outputs
    ett, asm = t.split(gi['ETT'], -1400), t.split(gi['ASM'], -1400)

    # Colour: tint where ETT.B says so, detail overlay, then ambient occlusion.
    colour = t.mix(gi['Base Colour'], t.mix(gi['Base Colour'], gi['Tint Colour'], 1.0, 'MULTIPLY', -1200),
                   ett['Blue'], x=-1000)
    detail = t.mix((1, 1, 1, 1), t.vec('SCALE', gi['Detail Map'], 2.0, -1200), gi['Detail Strength'], x=-1000)
    colour = t.mix(colour, detail, 1.0, 'MULTIPLY', -800)
    ao = t.math('MULTIPLY_ADD', t.math('SUBTRACT', asm['Red'], 1.0, x=-1200), gi['Colour AO Strength'], 1.0, x=-1000)
    colour = t.mix(colour, ao, 1.0, 'MULTIPLY', -600)
    reflection_ao = t.math('MULTIPLY_ADD', t.math('SUBTRACT', asm['Red'], 1.0, x=-1200),
                           gi['Reflection AO Strength'], 1.0, x=-1000)

    # Normal: two channels, Z rebuilt, packed back into a tangent-space colour.
    nrm = t.split(gi['Normal'], -1400)
    nx = t.math('MULTIPLY_ADD', t.math('MULTIPLY', nrm['Red'], gi['Normal Alpha'], x=-1200), 2.0, -1.0, x=-1000)
    ny = t.math('MULTIPLY_ADD', nrm['Green'], -2.0, 1.0, x=-1000)
    nz = t.math('SQRT', t.math('MAXIMUM', t.math('SUBTRACT', t.math('SUBTRACT', 1.0, t.math('MULTIPLY', nx, nx, x=-900), x=-800),
                                                  t.math('MULTIPLY', ny, ny, x=-900), x=-700), 0.0, x=-600), x=-500)
    combine = t.new('ShaderNodeCombineXYZ', -400)
    for axis, value in zip('XYZ', (nx, ny, nz)):
        t.set(combine.inputs[axis], value)
    packed = t.new('ShaderNodeVectorMath', -300, operation='MULTIPLY_ADD')
    packed.inputs[1].default_value = packed.inputs[2].default_value = (.5, .5, .5)
    t.set(packed.inputs[0], combine.outputs[0])
    normal_map = t.new('ShaderNodeNormalMap', -200)
    t.set(normal_map.inputs['Color'], packed.outputs[0])
    normal = normal_map.outputs['Normal']

    # Edge tint: brightest where the surface turns away from the camera.
    geometry = t.new('ShaderNodeNewGeometry', -400)
    facing = t.math('SUBTRACT', 1.0, t.vec('DOT_PRODUCT', normal, geometry.outputs['Incoming'], -100), x=0, clamp=True)
    rim = t.math('POWER', facing, gi['Edge Exponent'], x=100)
    edge = t.mix((0, 0, 0, 1), t.mix(colour, gi['Edge Tint Colour'], 1.0, 'MULTIPLY', 100), rim, x=200)
    colour = t.mix(colour, edge, 1.0, 'ADD', 300, clamp=True)

    # Emission: ETT red masks the emissive colour (mode 0) or the whole surface glows (mode 1).
    mask = t.math('MAXIMUM', ett['Red'], t.math('MINIMUM', gi['Emission Mode'], 1.0, x=-1200), x=-1000)
    emission = t.vec('SCALE', t.mix(gi['Emission Colour'], mask, 1.0, 'MULTIPLY', -800), gi['Emission Strength'], -600)
    emission = t.vec('ADD', emission, t.vec('SCALE', edge, gi['Edge Emission Strength'], 300), 400)

    opaque = t.math('LESS_THAN', gi['Alpha Cutoff'], 0.0, x=-1000)
    alpha = t.math('MAXIMUM', t.math('GREATER_THAN', gi['Alpha'], gi['Alpha Cutoff'], x=-1000), opaque, x=-800)

    bsdf = t.new('ShaderNodeBsdfPrincipled', 600)
    t.set(bsdf.inputs['Base Color'], colour)
    t.set(bsdf.inputs['Roughness'], t.math('SUBTRACT', 1.0, asm['Green'], x=-1000))
    t.set(bsdf.inputs['Metallic'], asm['Blue'])
    t.set(bsdf.inputs['Specular IOR Level'], t.math('MULTIPLY', reflection_ao, 0.5, x=-800))
    t.set(bsdf.inputs['Normal'], normal)
    t.set(bsdf.inputs['Alpha'], alpha)
    t.set(bsdf.inputs['Emission Color'], emission)
    bsdf.inputs['Emission Strength'].default_value = 1.0
    go = t.new('NodeGroupOutput', 900).inputs
    t.set(go['Shader'], bsdf.outputs['BSDF'])
    t.set(go['Edge Tint Preview'], edge)
    return tree


def build(mat, doc, image):
    textures, vectors = doc.get('textures', {}), doc.get('vectors', {}) or {}
    params = doc.get('parameters') or [[1, 1, 1, 1], [.7, 0, 1, 0], [1, 1, 0, 0], [0, 0, 0, 0]]
    tiling = params[2]
    cutoff = doc.get('alpha_cutoff', -1)
    cutoff = -1.0 if cutoff is None or cutoff < 0 else float(cutoff)
    mat.use_nodes = True
    mat.use_backface_culling = not doc.get('double_sided', False)
    mat['frostbite_game_shader'] = 'gw2'
    mat.node_tree.nodes.clear()
    t = _Builder(mat.node_tree)

    def rgba(name, default=(1.0, 1.0, 1.0)):
        v = vectors.get(name)
        return (v[0], v[1], v[2], 1.0) if v else (*default, 1.0)

    uvmap = t.new('ShaderNodeUVMap', -1350)
    tile = t.new('ShaderNodeVectorMath', -1150, operation='MULTIPLY_ADD')
    tile.inputs[1].default_value = (tiling[0], tiling[1], 1)
    tile.inputs[2].default_value = (tiling[2], 1 - tiling[1] - tiling[3], 0)
    t.set(tile.inputs[0], uvmap.outputs['UV'])

    def texture(role, non_color, label, coords=None):
        path = textures.get(role)
        img = image(path, non_color) if path else None
        if img is None:
            return None
        n = t.new('ShaderNodeTexImage', -850, label=label, image=img)
        t.set(n.inputs['Vector'], coords or tile.outputs['Vector'])
        return n

    node = t.new('ShaderNodeGroup', -100)
    node.node_tree = group()
    node.width = 240
    gin = node.inputs
    base = texture('base', False, 'Color')
    if base:
        t.set(gin['Base Colour'], base.outputs['Color'])
        t.set(gin['Alpha'], base.outputs['Alpha'])
    else:
        gin['Base Colour'].default_value = (*params[0][:3], 1.0)
    normal = texture('normal', True, 'Normal (X = R x A)')
    if normal:
        t.set(gin['Normal'], normal.outputs['Color'])
        t.set(gin['Normal Alpha'], normal.outputs['Alpha'])

    asm = texture('asm', True, 'ASM (occlusion, smoothness, metal)')
    ett = texture('ett', True, 'ETT (emissive, -, tint mask)')
    specular = None if asm else texture('specular_masks', True, 'Specular_Masks (smoothness, -, tint mask)')
    if asm:
        t.set(gin['ASM'], asm.outputs['Color'])
    elif specular:
        # Specular_Masks: R smoothness (x Smoothness_Scale), B tint mask, no occlusion or metal.
        sm = t.split(specular, -600)
        scale = (vectors.get('Smoothness_Scale') or [1.0])[0]
        packed = t.new('ShaderNodeCombineColor', -400, label='As ASM')
        packed.inputs['Red'].default_value = 1.0
        t.set(packed.inputs['Green'], t.math('MULTIPLY', sm['Red'], scale, x=-500))
        t.set(gin['ASM'], packed.outputs['Color'])
        if not ett:
            as_ett = t.new('ShaderNodeCombineColor', -400, label='As ETT')
            t.set(as_ett.inputs['Blue'], sm['Blue'])
            t.set(gin['ETT'], as_ett.outputs['Color'])
    else:
        surface = params[1]
        gin['ASM'].default_value = (1.0, 1.0 - float(surface[0]), float(surface[1]), 1.0)
    if ett:
        t.set(gin['ETT'], ett.outputs['Color'])

    def by_quadrant(names, singles, default):
        """Prop shaders: the colour for this UV quadrant. Others: their single colour."""
        if not any(n in vectors for n in names):
            return next((rgba(s) for s in singles if s in vectors), default)
        uv = t.new('ShaderNodeSeparateXYZ', -850)
        t.set(uv.inputs[0], uvmap.outputs['UV'])
        left = t.math('LESS_THAN', uv.outputs['X'], 1e-5, x=-650)       # u <= 0
        top = t.math('GREATER_THAN', uv.outputs['Y'], -1e-5, x=-650)    # Frostbite v <= 1 (Blender's V is flipped)
        q1, q2, q3, q4 = (rgba(n) for n in names)
        return t.mix(t.mix(q3, q4, left, x=-450), t.mix(q1, q2, left, x=-450), top, x=-300)

    tint = by_quadrant(QUADRANT_TINT, ('Tint_Color', 'Tint_Colour', 'ColorTint'), (1.0, 1.0, 1.0, 1.0))
    t.set(gin['Tint Colour'], tint)
    emissive = by_quadrant(QUADRANT_EMISSIVE, ('Emissive_Color', 'Param_Emissive_Color'), (0.0, 0.0, 0.0, 1.0))
    t.set(gin['Emission Colour'], emissive)

    edge = vectors.get('Edge_Tint') or vectors.get('Edge_Tint2')
    if edge:
        # The game stores the rim in HDR (values like 25 or 43); the hue goes on the node, scaled to 0..1.
        peak = max(max(edge[:3]), 1.0)
        gin['Edge Tint Colour'].default_value = (edge[0] / peak, edge[1] / peak, edge[2] / peak, 1.0)
        exponent = vectors.get('Edge_Tint_Exponent') or vectors.get('Edge_Tint_Exponent2')
        if exponent:
            gin['Edge Exponent'].default_value = exponent[0]

    detail_tiling = (vectors.get('Detail_UV_Tiling') or [1.0])[0] or 1.0
    scaled = t.vec('SCALE', tile.outputs['Vector'], detail_tiling, -1000)
    detail = texture('detail', True, 'Detail map', scaled)
    if detail:
        t.set(gin['Detail Map'], detail.outputs['Color'])
    else:
        mat.node_tree.nodes.remove(scaled.node)
    gin['Alpha Cutoff'].default_value = cutoff

    out = t.new('ShaderNodeOutputMaterial', 250)
    t.set(out.inputs['Surface'], node.outputs['Shader'])
    for n in t.nodes:
        n.select = False
    if base:
        base.select = True
        t.nodes.active = base
    else:
        # Solid/Texture view uses the active image; keep a white one there.
        img = t.new('ShaderNodeTexImage', -850, label='Color', image=placeholder('frostbite_white', (1, 1, 1, 1)))
        t.nodes.active = img
