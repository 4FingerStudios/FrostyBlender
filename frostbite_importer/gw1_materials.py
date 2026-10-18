"""GW1's legacy character and prop surfaces translated to Blender's BSDF.

PSA is Phong exponent / specular / ambient occlusion; SSO is smoothness /
specular / occlusion. Phong and Principled highlights are different models;
the exponent conversion preserves the lobe width rather than dropping it.
"""
import bpy

from .game_materials import _Nodes


def build(mat, doc, image):
    textures = doc.get('textures', {})
    named = doc.get('named_textures', {})
    vectors = doc.get('vectors') or {}
    shader = doc.get('shader_name', '').lower()
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    mat.use_backface_culling = not doc.get('double_sided', False)
    mat['frostbite_game_shader'] = 'gw1'
    t = _Nodes(mat)

    def scalar(name, default):
        value = vectors.get(name)
        return float(value[0]) if value else default

    def rgba(name, default=(1, 1, 1, 1)):
        value = vectors.get(name)
        return (*value[:3], 1) if value else default

    uv = t.new('ShaderNodeUVMap', -1500)
    params = doc.get('parameters') or [[], [], [1, 1, 0, 0]]
    tiling = params[2]
    tile = t.new('ShaderNodeVectorMath', -1300, operation='MULTIPLY_ADD')
    tile.inputs[1].default_value = (tiling[0], tiling[1], 1)
    tile.inputs[2].default_value = (tiling[2], 1-tiling[1]-tiling[3], 0)
    t.link(uv.outputs[0], tile.inputs[0])
    base = None

    def texture(path, non_color, label, coords=None):
        img = image(path, non_color) if path else None
        if not img:
            return None
        node = t.new('ShaderNodeTexImage', -1050, label=label, image=img)
        t.link(coords if coords is not None else tile.outputs[0], node.inputs['Vector'])
        return node

    uv_xy = t.new('ShaderNodeSeparateXYZ', -1300)
    t.link(uv.outputs[0], uv_xy.inputs[0])
    left = t.math('LESS_THAN', uv_xy.outputs['X'], 1e-5)
    top = t.math('GREATER_THAN', uv_xy.outputs['Y'], -1e-5)

    def quadrant(values):
        q1, q2, q3, q4 = values
        return t.mix(t.mix(q3, q4, left), t.mix(q1, q2, left), top)

    def sample(role, parameter, default, non_color=False):
        nonlocal base
        nodes = [texture(named.get('Q%d_%s' % (i, parameter)), non_color,
                         'Q%d %s' % (i, parameter)) for i in range(1, 5)]
        if any(nodes):
            if role == 'base':
                base = next(n for n in nodes if n)
            return quadrant([n.outputs['Color'] if n else default for n in nodes]), None
        node = texture(textures.get(role) or named.get(parameter), non_color, parameter)
        if role == 'base':
            base = node
        return (node.outputs['Color'], node) if node else (default, None)

    color, color_node = sample('base', 'Diffuse', rgba('Diffuse'))
    normal, normal_node = sample('normal', 'Normal', (.5, .5, 1, 1), True)
    if textures.get('normal') and textures.get('normal') == textures.get('base'):
        # Some prototype materials bind their diffuse as a missing-map placeholder.
        # Reading its painted colours as normal vectors corrupts surface lighting.
        normal_node = None
        normal = (.5, .5, 1, 1)
    tint = doc.get('tint') or rgba('Tint_Color', rgba('Tint_Colour'))
    if any('TintColour_Q%d' % i in vectors for i in range(1, 5)):
        tint = quadrant([rgba('TintColour_Q%d' % i) for i in range(1, 5)])
    color = t.mix(color, tint, 1, 'MULTIPLY')
    detail_scale = scalar('Detail_UV_Tiling', 1)
    detail_uv = t.new('ShaderNodeVectorMath', -1300, operation='SCALE')
    t.link(tile.outputs[0], detail_uv.inputs[0])
    detail_uv.inputs['Scale'].default_value = detail_scale
    detail = texture(textures.get('detail'), True, 'Detail', detail_uv.outputs[0])

    bsdf = t.new('ShaderNodeBsdfPrincipled', 600)
    out = t.new('ShaderNodeOutputMaterial', 900)
    t.link(bsdf.outputs['BSDF'], out.inputs['Surface'])
    psa_path = textures.get('psa')
    psa = texture(psa_path, True, 'PSA (Phong, specular, AO)') if psa_path != textures.get('base') else None
    masks, masks_node = sample('specular_masks', 'SpecularMasks', (1, 1, 1, 1), True)
    if not masks_node and named.get('Specular_Masks'):
        masks_node = texture(named['Specular_Masks'], True, 'SSO (smoothness, specular, AO)')
        masks = masks_node.outputs['Color']
    if psa:
        channels = t.split(psa)
        exponent = t.math('MAXIMUM', t.math('MULTIPLY', channels.outputs['Red'],
                                          255 * scalar('Smoothness', 1)), 1)
        roughness = t.math('SQRT', t.math('DIVIDE', 2, t.math('ADD', exponent, 2)))
        specular, ao = channels.outputs['Green'], channels.outputs['Blue']
    elif textures.get('specular_masks') or any('SpecularMasks' in key for key in named):
        channels = t.new('ShaderNodeSeparateColor', -600)
        t.set(channels.inputs['Color'], masks)
        roughness = t.math('SUBTRACT', 1, t.math('MINIMUM', t.math('MULTIPLY',
                           channels.outputs['Red'], scalar('Smoothness_Scale', 1)), 1))
        specular, ao = channels.outputs['Green'], channels.outputs['Blue']
    else:
        roughness = max(0, 1-min(1, scalar('Smoothness', .3)))
        specular, ao = 1, (1, 1, 1, 1)
    t.set(bsdf.inputs['Roughness'], roughness)
    t.set(bsdf.inputs['Specular IOR Level'], t.math('MULTIPLY', specular,
          scalar('Specular_Scale', scalar('Specular', .5))))
    t.set(bsdf.inputs['Base Color'], t.mix(color, ao, 1, 'MULTIPLY'))

    if normal_node or any(key.startswith('Q') and key.endswith('_Normal') for key in named):
        sep = t.new('ShaderNodeSeparateColor', -600)
        t.set(sep.inputs['Color'], normal)
        x = t.math('MULTIPLY_ADD', sep.outputs['Red'], 2, -1)
        y = t.math('MULTIPLY_ADD', sep.outputs['Green'], -2, 1)
        z = t.math('SQRT', t.math('MAXIMUM', t.math('SUBTRACT', 1,
                   t.math('ADD', t.math('MULTIPLY', x, x), t.math('MULTIPLY', y, y))), 0))
        vec = t.new('ShaderNodeCombineXYZ', 0)
        for name, value in zip(('X', 'Y', 'Z'), (x, y, z)):
            t.link(value, vec.inputs[name])
        pack = t.new('ShaderNodeVectorMath', 200, operation='MULTIPLY_ADD')
        pack.inputs[1].default_value = pack.inputs[2].default_value = (.5, .5, .5)
        t.link(vec.outputs[0], pack.inputs[0])
        nmap = t.new('ShaderNodeNormalMap', 400)
        t.link(pack.outputs[0], nmap.inputs['Color'])
        t.link(nmap.outputs[0], bsdf.inputs['Normal'])

    if detail:
        # GW1's black/white velvet grain changes the surface, not its paint.
        bump = t.new('ShaderNodeBump', 500, label='Detail grain')
        bump.inputs['Strength'].default_value = scalar('Detail_Strength', .15)
        bump.inputs['Distance'].default_value = .003
        t.link(detail.outputs['Color'], bump.inputs['Height'])
        if bsdf.inputs['Normal'].is_linked:
            t.link(bsdf.inputs['Normal'].links[0].from_socket, bump.inputs['Normal'])
        t.link(bump.outputs['Normal'], bsdf.inputs['Normal'])

    opacity = scalar('Opacity', 1)
    alpha = color_node.outputs['Alpha'] if color_node else 1
    cutoff = doc.get('alpha_cutoff', -1)
    cutoff = -1 if cutoff is None else cutoff
    if cutoff >= 0 or 'alphatest' in shader or 'foliage' in shader:
        alpha = t.math('GREATER_THAN', alpha, scalar('AlphaTestThreshold', cutoff if cutoff >= 0 else .5))
    elif 'alphablend' not in shader and 'glass' not in shader:
        alpha = 1
    t.set(bsdf.inputs['Alpha'], t.math('MULTIPLY', alpha, opacity))
    if hasattr(mat, 'surface_render_method'):
        mat.surface_render_method = 'DITHERED'
    if 'glass' in shader:
        bsdf.inputs['Transmission Weight'].default_value = 1
        bsdf.inputs['IOR'].default_value = scalar('IOR', 1.45)
        if hasattr(mat, 'use_transparency_overlap'):
            mat.use_transparency_overlap = False
    if 'velvet' in shader:
        bsdf.inputs['Sheen Weight'].default_value = scalar('Sheen_Weight', .25)
        edge = rgba('Edge_Tint')
        magnitude = max(1, *edge[:3])
        bsdf.inputs['Sheen Tint'].default_value = tuple(c/magnitude for c in edge[:3]) + (1,)

    light = texture(textures.get('light_mask'), True, 'Animated lights')
    if light:
        columns = max(1, round(scalar('Lights_Texture_Columns', 1)))
        rows = max(1, round(scalar('Lights_Texture_Rows', 1)))
        frame = t.new('ShaderNodeValue', -1500, label='Light atlas frame')
        fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
        frame.outputs[0].driver_add('default_value').driver.expression = (
            'floor((frame-1)*%r/%r) %% %d' % (scalar('Lights_Speed', 6), fps, columns*rows))
        offset = t.new('ShaderNodeCombineXYZ', -1300)
        t.link(t.math('DIVIDE', t.math('MODULO', frame.outputs[0], columns), columns), offset.inputs['X'])
        t.link(t.math('SUBTRACT', 1-1/rows,
                     t.math('DIVIDE', t.math('FLOOR', t.math('DIVIDE', frame.outputs[0], columns)), rows)), offset.inputs['Y'])
        atlas = t.new('ShaderNodeVectorMath', -1200, operation='MULTIPLY_ADD')
        t.link(tile.outputs[0], atlas.inputs[0])
        atlas.inputs[1].default_value = (1/columns, 1/rows, 1)
        t.link(offset.outputs[0], atlas.inputs[2])
        t.link(atlas.outputs[0], light.inputs['Vector'])
        t.link(t.mix(color, light.outputs['Color'], 1, 'MULTIPLY'), bsdf.inputs['Emission Color'])
        bsdf.inputs['Emission Strength'].default_value = scalar('Lights_Color_Strength', 1)
    else:
        emissive = texture(textures.get('emissive'), False, 'Emissive')
        if emissive:
            t.link(emissive.outputs['Color'], bsdf.inputs['Emission Color'])
            bsdf.inputs['Emission Strength'].default_value = scalar('Emissive_Strength', 1)
    for node in t.nodes:
        node.select = False
    if base:
        base.select = True
        t.nodes.active = base
    mat.diffuse_color = rgba('Diffuse') if base is None else (1, 1, 1, opacity)
