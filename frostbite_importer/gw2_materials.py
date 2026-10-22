"""Garden Warfare 2 materials around one shared node group, "PVZ GW2 Generic".

The group takes the game's textures as they are and does what GW2's GBuffer shaders do with them:
  Normal   X = R * A, Y = G (DirectX green, flipped for Blender), Z rebuilt
  ASM      R ambient occlusion, G smoothness, B metalness
  ETT      R emissive mask, B tint mask: colour = lerp(Color, Color * Tint, ETT.B) (Velvet: ETT.G, repacked)
  Edge     colour = lerp(Color, Color * Edge_Tint, (1 - N.V) ^ Edge_Tint_Exponent * AO * mask), white = off;
           Velvet masks it with Detail_Map.R (tiled by Detail_UV_Tiling) and ETT.B
  Goop     g = max(SuperGooped, Gooped, DirtGooped) * Goop_Mask.R; smoothness = lerp(ASM.G, 0.386, g);
           colour = lerp(colour + Buff_Colour * Buff_Strength * 0.67 * min((1.001 - N.V) ^ 0.05, 1), goop colour, g)
           (green when SuperGooped, brown and scaled by saturate((MuckBuff_Strength - 0.8) * 5) when DirtGooped,
           else purple). These are gameplay states, all 0 in the materials, so the group inputs start off.
Citron's peel shader has its own small group in front ("PVZ GW2 Citron"): where Color.A is 0 the flesh shows
through, roughly Color * Color * ice depth, which turns the pale texture deep orange as in the game. The flesh
texture is read twice, the second time shifted by a parallax offset ("PVZ GW2 Citron Parallax").
Emissive_Sequence shaders light Color by a Light_Mask strip whose cell moves with time ("PVZ GW2 Light Sequence").
Every GW2 material uses the same group, so EEVEE compiles one shader for all of them, and each material's
values sit on the group node where they can be changed. Specular_Masks shaders (R smoothness, B tint mask)
are repacked into ASM/ETT outside the group. Prop shaders pick tint and emissive colours by UV quadrant.
"""
import bpy

from .materials import placeholder

GROUP = 'PVZ GW2 Generic'
VERSION = 4
CITRON = 'PVZ GW2 Citron'
CITRON_PARALLAX = 'PVZ GW2 Citron Parallax'
SEQUENCE = 'PVZ GW2 Light Sequence'
GOOP = {'Super Gooped': 'SuperGooped', 'Gooped': 'Gooped', 'Dirt Gooped': 'DirtGooped',
        'Buff Strength': 'Buff_Strength', 'Muck Buff Strength': 'MuckBuff_Strength'}
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
    ('Edge Tint Colour', 'NodeSocketColor', (1, 1, 1, 1)),
    ('Edge Exponent', 'NodeSocketFloat', 4.0),
    ('Edge Mask', 'NodeSocketFloat', 1.0),
    ('Edge Emission Strength', 'NodeSocketFloat', 0.0),
    ('Detail Map', 'NodeSocketColor', (1, 1, 1, 1)),
    ('Detail Strength', 'NodeSocketFloat', 1.0),
    ('Emission Colour', 'NodeSocketColor', (0, 0, 0, 1)),
    ('Emission Strength', 'NodeSocketFloat', 1.0),
    ('Emission Mode', 'NodeSocketInt', 0),              # 0: ETT red masks the emission, 1: whole surface
    ('Colour AO Strength', 'NodeSocketFloat', 0.35),     # the game only dims ambient light with AO
    ('Reflection AO Strength', 'NodeSocketFloat', 1.0),
    ('Alpha Cutoff', 'NodeSocketFloat', -1.0),          # below 0: opaque
    ('Goop Mask', 'NodeSocketFloat', 1.0),
    ('Super Gooped', 'NodeSocketFloat', 0.0),
    ('Gooped', 'NodeSocketFloat', 0.0),
    ('Dirt Gooped', 'NodeSocketFloat', 0.0),
    ('Muck Buff Strength', 'NodeSocketFloat', 0.0),
    ('Buff Colour', 'NodeSocketColor', (0, 0, 0, 1)),
    ('Buff Strength', 'NodeSocketFloat', 0.0),
]
TIPS = {
    'ETT': 'R emissive mask, B tint mask',
    'ASM': 'R ambient occlusion, G smoothness, B metalness',
    'Normal': 'Game normal map: X from R x Normal Alpha, Y from G',
    'Edge Tint Colour': 'Multiplies the colour at grazing angles (game values go above 1). White: no edge tint',
    'Edge Mask': 'Where the edge tint applies (Velvet: ETT blue)',
    'Detail Map': 'Velvet detail: red channel masks the edge tint',
    'Emission Mode': '0: ETT red masks the emission. 1: the whole surface glows',
    'Alpha Cutoff': 'Alpha test threshold. Below 0 the surface is opaque',
    'Goop Mask': 'Where goop can cover the surface (Goop_Mask red)',
    'Super Gooped': 'Above 0: green goop (Super Goop) over the masked surface',
    'Gooped': 'Above 0: purple goop over the masked surface',
    'Dirt Gooped': 'Above 0: brown muck, faded in by Muck Buff Strength from 0.8 to 1',
    'Buff Colour': 'Rim glow colour added while buffed',
    'Buff Strength': 'How strong the buff rim glow is. 0: off',
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
        if isinstance(colour, bpy.types.Node):  # an image texture node
            colour = colour.outputs['Color']
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

    # Colour: edge tint (below), tint where ETT.B says so, then ambient occlusion.
    colour = gi['Base Colour']
    ao = t.math('MULTIPLY_ADD', t.math('SUBTRACT', asm['Red'], 1.0, x=-1200), gi['Colour AO Strength'], 1.0, x=-1000)
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

    # Edge tint: the colour times Edge Tint, strongest where the surface turns away from the camera.
    geometry = t.new('ShaderNodeNewGeometry', -400)
    facing = t.math('SUBTRACT', 1.0, t.vec('DOT_PRODUCT', normal, geometry.outputs['Incoming'], -100), x=0, clamp=True)
    rim = t.math('POWER', facing, gi['Edge Exponent'], x=100)
    detail = t.math('MULTIPLY_ADD', t.math('SUBTRACT', t.split(gi['Detail Map'], -1400)['Red'], 1.0, x=-1200),
                    gi['Detail Strength'], 1.0, x=-1000)
    amount = t.math('MULTIPLY', t.math('MULTIPLY', rim, asm['Red'], x=150), t.math('MULTIPLY', gi['Edge Mask'], detail, x=150), x=200)
    plain = colour
    colour = t.mix(colour, t.mix(colour, gi['Edge Tint Colour'], 1.0, 'MULTIPLY', 200), amount, x=250, clamp=True)
    edge = t.mix(colour, plain, 1.0, 'SUBTRACT', 300, clamp=True)
    colour = t.mix(colour, t.mix(colour, gi['Tint Colour'], 1.0, 'MULTIPLY', 300), ett['Blue'], x=350)

    # Goop and buff, gameplay states that start off: the buff adds a rim glow, then goop covers the masked surface.
    buff_rim = t.math('MINIMUM', t.math('POWER', t.math('ADD', facing, 0.001, x=100), 0.05, x=150), 1.0, x=200)
    buff = t.math('MULTIPLY', t.math('MULTIPLY', buff_rim, gi['Buff Strength'], x=250), 0.67, x=300)
    colour = t.mix(colour, gi['Buff Colour'], buff, 'ADD', 380)
    goop = t.math('MULTIPLY', t.math('MAXIMUM', t.math('MAXIMUM', gi['Super Gooped'], gi['Gooped'], x=-1200),
                                     gi['Dirt Gooped'], x=-1100), gi['Goop Mask'], x=-1000)
    dirt = t.math('GREATER_THAN', gi['Dirt Gooped'], 0.0, x=-1100)
    muck = t.math('MULTIPLY', goop, t.math('MULTIPLY_ADD', gi['Muck Buff Strength'], 5.0, -4.0, x=-1000, clamp=True), x=-900)
    covered = t.math('MULTIPLY_ADD', dirt, t.math('SUBTRACT', muck, goop, x=-800), goop, x=-700)
    goop_colour = t.mix((0.286, 0.016, 0.619, 1), (0.154, 0.73, 0.007, 1),
                        t.math('GREATER_THAN', gi['Super Gooped'], 0.0, x=-1100), x=-700)
    goop_colour = t.mix(goop_colour, (0.445201, 0.176755, 0.039334, 1), dirt, x=-600)
    colour = t.mix(colour, goop_colour, covered, x=420)
    smoothness = t.math('MULTIPLY_ADD', goop, t.math('SUBTRACT', 0.386429, asm['Green'], x=-1200), asm['Green'], x=-1100)
    colour = t.mix(colour, ao, 1.0, 'MULTIPLY', 460)

    # Emission: ETT red masks the emissive colour (mode 0) or the whole surface glows (mode 1).
    mask = t.math('MAXIMUM', ett['Red'], t.math('MINIMUM', gi['Emission Mode'], 1.0, x=-1200), x=-1000)
    emission = t.vec('SCALE', t.mix(gi['Emission Colour'], mask, 1.0, 'MULTIPLY', -800), gi['Emission Strength'], -600)
    emission = t.vec('ADD', emission, t.vec('SCALE', edge, gi['Edge Emission Strength'], 300), 400)

    opaque = t.math('LESS_THAN', gi['Alpha Cutoff'], 0.0, x=-1000)
    alpha = t.math('MAXIMUM', t.math('GREATER_THAN', gi['Alpha'], gi['Alpha Cutoff'], x=-1000), opaque, x=-800)

    bsdf = t.new('ShaderNodeBsdfPrincipled', 600)
    t.set(bsdf.inputs['Base Color'], colour)
    t.set(bsdf.inputs['Roughness'], t.math('SUBTRACT', 1.0, smoothness, x=-1000))
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


CITRON_INPUTS = [
    ('Color', 'NodeSocketColor', (1, 1, 1, 1)),
    ('Peel Mask', 'NodeSocketFloat', 1.0),           # Color alpha: 1 peel, 0 see-through flesh
    ('Pulp', 'NodeSocketFloat', 0.25),               # Material2_Color alpha
    ('Parallax Pulp', 'NodeSocketFloat', 0.25),      # Material2_Color alpha at the parallax-shifted UV
    ('AO', 'NodeSocketFloat', 1.0),
    ('Edge Tint', 'NodeSocketColor', (1, 1, 1, 1)),
    ('Edge Exponent', 'NodeSocketFloat', 6.0),
    # The defaults below are the shader's own, for values the materials leave out.
    ('Parallax Tint', 'NodeSocketColor', (.35, .35, .35, 1)),
    ('Ice Depth Exponent', 'NodeSocketFloat', 1.0),
    ('Ice Depth Min', 'NodeSocketFloat', 0.0),
    ('Ice Depth 2 Exponent', 'NodeSocketFloat', 1.0),
    ('Ice Depth 2 Min', 'NodeSocketFloat', 0.0),
]
CITRON_PARALLAX_INPUTS = [
    ('UV', 'NodeSocketVector', (0, 0, 0)),
    ('Pulp', 'NodeSocketFloat', 0.25),
    ('Color Red', 'NodeSocketFloat', 1.0),
    ('Parallax Height', 'NodeSocketFloat', 0.1),
    ('Parallax Height Blend', 'NodeSocketFloat', 0.263),
    ('Parallax Scale', 'NodeSocketFloat', 0.1),
]
SEQUENCE_INPUTS = [
    ('UV', 'NodeSocketVector', (0, 0, 0)),
    ('Rows', 'NodeSocketFloat', 1.0),
    ('Columns', 'NodeSocketFloat', 1.0),
    ('Blink Speed', 'NodeSocketFloat', 4.0),
]


def _new_group(name, inputs, outputs):
    tree = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    tree['frostbite_version'] = VERSION
    for socket_name, kind, default in inputs:
        tree.interface.new_socket(socket_name, in_out='INPUT', socket_type=kind).default_value = default
    for socket_name, kind in outputs:
        tree.interface.new_socket(socket_name, in_out='OUTPUT', socket_type=kind)
    return tree


def citron_parallax_group():
    """The Citron shader's flesh UV: the Material2_Color lookup moves along the view, by how deep the flesh is.
      h  = lerp(pulp, C.r, Parallax_Height_Blend)
      uv = uv + view.xy * (2 * h * Parallax_Height - 1) * Parallax_Scale     view in tangent space
    """
    found = bpy.data.node_groups.get(CITRON_PARALLAX)
    if found and found.get('frostbite_version') == VERSION:
        return found
    tree = _new_group(CITRON_PARALLAX, CITRON_PARALLAX_INPUTS, [('UV', 'NodeSocketVector')])
    t = _Builder(tree)
    gi = t.new('NodeGroupInput', -1000).outputs
    height = t.math('MULTIPLY_ADD', t.math('SUBTRACT', gi['Color Red'], gi['Pulp'], x=-800), gi['Parallax Height Blend'],
                    gi['Pulp'], x=-700)
    depth = t.math('MULTIPLY', t.math('MULTIPLY_ADD', t.math('MULTIPLY', height, gi['Parallax Height'], x=-600), 2.0, -1.0,
                                      x=-500), gi['Parallax Scale'], x=-400)
    # Tangent space in Blender's UV directions: U along the tangent, V along normal x tangent.
    geometry = t.new('ShaderNodeNewGeometry', -800)
    tangent = t.new('ShaderNodeTangent', -800, direction_type='UV_MAP')
    bitangent = t.vec('CROSS_PRODUCT', geometry.outputs['Normal'], tangent.outputs['Tangent'], -600)
    view = geometry.outputs['Incoming']
    offset = t.new('ShaderNodeCombineXYZ', -400)
    t.set(offset.inputs['X'], t.vec('DOT_PRODUCT', view, tangent.outputs['Tangent'], -500))
    t.set(offset.inputs['Y'], t.vec('DOT_PRODUCT', view, bitangent, -500))
    uv = t.vec('ADD', gi['UV'], t.vec('SCALE', offset.outputs[0], depth, -300), -200)
    t.set(t.new('NodeGroupOutput', 0).inputs['UV'], uv)
    return tree


def sequence_group():
    """PBR_Character_Main_Emissive_Sequence's Light_Mask UV: a strip of Rows x Columns cells stepped through in time.
      frame = ceil((seconds mod 10) * Lights_Blink_Speed)
      uv    = uv + (floor(frame / Columns) / Rows, frame / Columns)      (Frostbite's V; Blender's runs the other way)
    Seconds come from the scene frame through a driver.
    """
    found = bpy.data.node_groups.get(SEQUENCE)
    if found and found.get('frostbite_version') == VERSION:
        return found
    tree = _new_group(SEQUENCE, SEQUENCE_INPUTS, [('UV', 'NodeSocketVector')])
    t = _Builder(tree)
    gi = t.new('NodeGroupInput', -1000).outputs
    seconds = t.new('ShaderNodeValue', -1000, label='Seconds (driven by the frame)')
    seconds.name = 'Seconds'
    fcurve = seconds.outputs[0].driver_add('default_value')
    driver = fcurve.driver
    driver.type = 'SCRIPTED'
    for name, path in (('fps', 'render.fps'), ('base', 'render.fps_base')):
        var = driver.variables.new()
        var.name, var.type = name, 'SINGLE_PROP'
        var.targets[0].id_type = 'SCENE'
        var.targets[0].id = bpy.context.scene
        var.targets[0].data_path = path
    driver.expression = 'frame * base / fps'
    frame = t.math('CEIL', t.math('MULTIPLY', t.math('MODULO', seconds.outputs[0], 10.0, x=-800), gi['Blink Speed'],
                                     x=-700), x=-600)
    cell = t.math('DIVIDE', frame, gi['Columns'], x=-500)
    offset = t.new('ShaderNodeCombineXYZ', -300)
    t.set(offset.inputs['X'], t.math('DIVIDE', t.math('FLOOR', cell, x=-400), gi['Rows'], x=-350))
    t.set(offset.inputs['Y'], t.math('MULTIPLY', cell, -1.0, x=-400))
    t.set(t.new('NodeGroupOutput', 0).inputs['UV'], t.vec('ADD', gi['UV'], offset.outputs[0], -200))
    return tree


def citron_group():
    """PBR_Character_Main_Citron's colour, read from its GBuffer shader:
      tint   = Parallax_Tint * pulp'                                         pulp' read at the parallax UV
      inner  = lerp(tint, lerp(C, max(pulp, C), pulp), pulp)
      edged  = lerp(C, C * Edge_Tint, f ^ Edge_Tint_Exponent * AO)          f = 1 - N.V
      colour = lerp(edged * (inner * (f ^ Ice_Depth_Exponent + Ice_Depth_min) + tint), C, C.a)
               + (f ^ Ice_Depth2 + Ice_Depth2_min) * C
      glow   = tint * (1 - C.a), added to ETT.R before the emissive colour multiplies them
    """
    found = bpy.data.node_groups.get(CITRON)
    if found and found.get('frostbite_version') == VERSION:
        return found
    tree = _new_group(CITRON, CITRON_INPUTS, [('Colour', 'NodeSocketColor'), ('Glow', 'NodeSocketColor')])
    t = _Builder(tree)
    gi = t.new('NodeGroupInput', -1200).outputs
    c, pulp = gi['Color'], gi['Pulp']
    geometry = t.new('ShaderNodeNewGeometry', -1200)
    f = t.math('SUBTRACT', 1.0, t.vec('DOT_PRODUCT', geometry.outputs['Normal'], geometry.outputs['Incoming'], -1000),
               x=-900, clamp=True)
    raised = t.mix(c, t.mix(c, pulp, 1.0, 'LIGHTEN', -800), pulp, x=-700)
    tint = t.mix(gi['Parallax Tint'], gi['Parallax Pulp'], 1.0, 'MULTIPLY', -800)
    inner = t.mix(tint, raised, pulp, x=-600)
    rim = t.math('MULTIPLY', t.math('POWER', f, gi['Edge Exponent'], x=-800), gi['AO'], x=-700)
    edged = t.mix(c, t.mix(c, gi['Edge Tint'], 1.0, 'MULTIPLY', -700), rim, x=-600)
    ice = t.math('ADD', t.math('POWER', f, gi['Ice Depth Exponent'], x=-800), gi['Ice Depth Min'], x=-700)
    depth = t.mix(t.mix(inner, ice, 1.0, 'MULTIPLY', -500), tint, 1.0, 'ADD', -450)
    flesh = t.mix(edged, depth, 1.0, 'MULTIPLY', -400)
    colour = t.mix(flesh, c, gi['Peel Mask'], x=-300)
    ice2 = t.math('ADD', t.math('POWER', f, gi['Ice Depth 2 Exponent'], x=-800), gi['Ice Depth 2 Min'], x=-700)
    colour = t.mix(colour, t.mix(c, ice2, 1.0, 'MULTIPLY', -400), 1.0, 'ADD', -200, clamp=True)
    go = t.new('NodeGroupOutput', 0).inputs
    t.set(go['Colour'], colour)
    t.set(go['Glow'], t.mix(tint, (0, 0, 0, 1), gi['Peel Mask'], x=-300))
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

    named = doc.get('named_textures', {}) or {}

    def texture(role, non_color, label, coords=None):
        path = textures.get(role) or named.get(role)
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
    velvet = 'Edge_Tint2' in vectors
    if ett and velvet:
        # Velvet: tint mask in ETT.G and the edge tint masked by ETT.B.
        et = t.split(ett, -600)
        packed = t.new('ShaderNodeCombineColor', -400, label='ETT for Velvet')
        t.set(packed.inputs['Red'], et['Red'])
        t.set(packed.inputs['Green'], et['Green'])
        t.set(packed.inputs['Blue'], et['Green'])
        t.set(gin['ETT'], packed.outputs['Color'])
        t.set(gin['Edge Mask'], et['Blue'])
    elif ett:
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
    sequence = 'Lights_Blink_Speed' in vectors or doc.get('shader_name', '').endswith('_Emissive_Sequence')
    if sequence and base:
        # Emissive_Sequence: emission = Color * Light_Mask.R (at the time-stepped cell) * Emissive_Color^2, ETT.R masked.
        seq = t.new('ShaderNodeGroup', -1150, label='Light sequence')
        seq.node_tree = sequence_group()
        t.set(seq.inputs['UV'], tile.outputs['Vector'])
        for socket, name in (('Rows', 'Lights_Texture_Rows'), ('Columns', 'Lights_Texture_Columns'),
                             ('Blink Speed', 'Lights_Blink_Speed')):
            if name in vectors:
                seq.inputs[socket].default_value = vectors[name][0]
        lights = texture('light_mask', True, 'Light_Mask (sequence)', seq.outputs['UV'])
        if lights and isinstance(emissive, tuple):
            squared = tuple(v * v for v in emissive[:3]) + (1.0,)
            lit = t.mix(t.mix(base.outputs['Color'], squared, 1.0, 'MULTIPLY', -450), t.split(lights, -600)['Red'],
                        1.0, 'MULTIPLY', -300)
            t.set(gin['Emission Colour'], lit)
    for socket, name in GOOP.items():
        if name in vectors:
            gin[socket].default_value = vectors[name][0]
    if 'Buff_Colour' in vectors:
        gin['Buff Colour'].default_value = rgba('Buff_Colour', (0.0, 0.0, 0.0))
    goop = texture('Goop_Mask', True, 'Goop_Mask (red)')
    if goop:
        t.set(gin['Goop Mask'], t.split(goop, -600)['Red'])

    edge =vectors.get('Edge_Tint') or vectors.get('Edge_Tint2')
    exponent = vectors.get('Edge_Tint_Exponent') or vectors.get('Edge_Tint_Exponent2')
    citron = 'Ice_Depth2_min' in vectors or doc.get('shader_name', '').endswith('_Citron')
    if citron and base:
        # The peel's see-through flesh is worked out in front of the main group; its edge tint goes there too.
        ice = t.new('ShaderNodeGroup', -450, label='Citron peel and flesh')
        ice.node_tree = citron_group()
        ice.width = 220
        t.set(ice.inputs['Color'], base.outputs['Color'])
        t.set(ice.inputs['Peel Mask'], base.outputs['Alpha'])
        if asm:
            t.set(ice.inputs['AO'], t.split(asm, -600)['Red'])
        scale = (vectors.get('Parallax_UV_Scale') or [1.0])[0] or 1.0
        pulp_uv = t.vec('SCALE', tile.outputs['Vector'], scale, -1000)
        pulp = texture('Material2_Color', False, 'Material2_Color (pulp in alpha)', pulp_uv)
        if pulp:
            # The flesh read again, shifted by how deep it is along the view.
            parallax = t.new('ShaderNodeGroup', -700, label='Citron parallax')
            parallax.node_tree = citron_parallax_group()
            t.set(parallax.inputs['UV'], pulp_uv)
            t.set(parallax.inputs['Pulp'], pulp.outputs['Alpha'])
            t.set(parallax.inputs['Color Red'], t.split(base, -600)['Red'])
            for socket, name in (('Parallax Height', 'Parallax_Height'), ('Parallax Height Blend', 'Parallax_Height_Blend'),
                                 ('Parallax Scale', 'Parallax_Scale')):
                if name in vectors:
                    parallax.inputs[socket].default_value = vectors[name][0]
            shifted = texture('Material2_Color', False, 'Material2_Color at the parallax UV', parallax.outputs['UV'])
            t.set(ice.inputs['Pulp'], pulp.outputs['Alpha'])
            t.set(ice.inputs['Parallax Pulp'], shifted.outputs['Alpha'])
        # Emission: (glow + ETT.R) x the emissive colour, so the whole surface takes part.
        glow = ice.outputs['Glow']
        if ett:
            glow = t.mix(glow, t.split(ett, -600)['Red'], 1.0, 'ADD', -300)
        t.set(gin['Emission Colour'], t.mix(glow, emissive, 1.0, 'MULTIPLY', -250))
        gin['Emission Mode'].default_value = 1
        if edge:
            ice.inputs['Edge Tint'].default_value = (*edge[:3], 1.0)
        if exponent:
            ice.inputs['Edge Exponent'].default_value = exponent[0]
        for socket, name in (('Parallax Tint', 'Parallax_Tint'), ('Ice Depth Exponent', 'Ice_Depth_Exponent'),
                             ('Ice Depth Min', 'Ice_Depth_min'), ('Ice Depth 2 Exponent', 'Ice_Depth2'),
                             ('Ice Depth 2 Min', 'Ice_Depth2_min')):
            if name in vectors:
                v = vectors[name]
                ice.inputs[socket].default_value = (*v[:3], 1.0) if socket == 'Parallax Tint' else v[0]
        # This shader samples with mirror addressing: most of the head's UVs sit in the (-1, -1) tile, and
        # repeating there instead turns the peel segments sideways and loses the face.
        for n in t.nodes:
            if n.type == 'TEX_IMAGE':
                n.extension = 'MIRROR'
        t.set(gin['Base Colour'], ice.outputs['Colour'])
        gin['Alpha'].default_value = 1.0
        t.links.remove(gin['Alpha'].links[0])
    elif edge:
        # Game values, above 1 (4.9, 25, 43): the colour is multiplied by this much at the edges.
        gin['Edge Tint Colour'].default_value = (*edge[:3], 1.0)
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
