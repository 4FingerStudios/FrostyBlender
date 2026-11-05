"""Alpha-blended decal surfaces ('surface': 'decal'): Battle for Neighborville decal volumes,
projected onto the map by the converter (tools/bfn_decals.py), and Garden Warfare terrain decals such as
roads (tools/terrain_decals.py); and Battle for Neighborville decal strips laid over props ('surface': 'prop_decal',
tools/convert_legacy.py bfn_prop_decal)."""
import bpy

from .game_materials import _Nodes


def build(mat, doc, image):
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    mat.use_backface_culling = False
    mat['frostbite_game_shader'] = 'decal'
    mat['frostbite_source_shader'] = doc.get('source_decal_template', '')
    mat['frostbite_surface_approximation'] = doc.get('surface_approximation', '')
    if hasattr(mat, 'surface_render_method'):
        mat.surface_render_method = 'BLENDED'
    if hasattr(mat, 'use_transparent_shadow'):
        mat.use_transparent_shadow = True
    tint = tuple(doc.get('tint', [1, 1, 1, 1]))
    mat.diffuse_color = tint
    t = _Nodes(mat)
    uv = t.new('ShaderNodeUVMap', -900)
    # Decal volumes show their texture once; terrain decals (roads) repeat it along their length.
    tex = t.new('ShaderNodeTexImage', -650, label='Decal colour', extension=doc.get('texture_extension', 'CLIP'))
    tex.image = image(doc.get('textures', {}).get('base'), False)
    t.link(uv.outputs['UV'], tex.inputs['Vector'])
    color = t.mix(tex.outputs['Color'], tint, 1, 'MULTIPLY', x=-350)
    bsdf = t.new('ShaderNodeBsdfPrincipled', -100, label='Decal')
    t.link(color, bsdf.inputs['Base Color'])
    t.link(tex.outputs['Alpha'], bsdf.inputs['Alpha'])
    bsdf.inputs['Roughness'].default_value = .6
    out = t.new('ShaderNodeOutputMaterial', 200)
    t.link(bsdf.outputs['BSDF'], out.inputs['Surface'])
    for node in t.nodes:
        node.select = False
    tex.select = True
    t.nodes.active = tex


def build_prop(mat, doc, image):
    """A GW3_Prop_Decal strip: the __Decal atlas holds normal X and Y (red, green), a shade (blue) and a mask
    (alpha) whose lower level is the seam band and whose upper level is the stitches. LerpEverything strips are
    painted their decal colour (by UV quadrant; the stitches DecalColor2); LerpNormal strips only change the
    normal, which Blender cannot blend into the prop below, so they darken it where their normal tilts."""
    from .materials import quadrant_colour
    info = doc.get('prop_decal') or {}
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    mat.use_backface_culling = not doc.get('double_sided', False)
    mat['frostbite_game_shader'] = 'prop_decal_' + info.get('mode', 'everything')
    mat['frostbite_source_shader'] = doc.get('source_decal_template', '')
    mat['frostbite_surface_approximation'] = doc.get('surface_approximation', '')
    if hasattr(mat, 'surface_render_method'):
        mat.surface_render_method = 'BLENDED'
    if hasattr(mat, 'use_transparent_shadow'):
        mat.use_transparent_shadow = True
    t = _Nodes(mat)
    uv = t.new('ShaderNodeUVMap', -1300)
    tex = t.new('ShaderNodeTexImage', -1100, label='Decal atlas')
    tex.image = image(doc.get('textures', {}).get('decal'), True)
    t.link(uv.outputs['UV'], tex.inputs['Vector'])
    rgb = t.split(tex, -900)

    def ramp(value, low, high, x):
        n = t.new('ShaderNodeMapRange', x, clamp=True)
        t.link(value, n.inputs['Value'])
        n.inputs['From Min'].default_value, n.inputs['From Max'].default_value = low, high
        return n.outputs['Result']
    cover = ramp(tex.outputs['Alpha'], .05, .3, -700)
    # The normal's Z rebuilt from X and Y; X in red, Y in green negated (as on props).
    nx = t.math('MULTIPLY_ADD', rgb.outputs['Red'], 2.0, -1.0, x=-700)
    ny = t.math('MULTIPLY_ADD', rgb.outputs['Green'], -2.0, 1.0, x=-700)
    xy = t.math('ADD', t.math('MULTIPLY', nx, nx, x=-550), t.math('MULTIPLY', ny, ny, x=-550), x=-450)
    nz = t.math('SQRT', t.math('MAXIMUM', t.math('SUBTRACT', 1.0, xy, x=-350), 0.0, x=-250), x=-150)
    shade = t.math('MULTIPLY', nz, rgb.outputs['Blue'], x=-50)
    out = t.new('ShaderNodeOutputMaterial', 500)
    if info.get('mode') == 'normal':
        # Seen through: the prop below, darkened where the strip's bevel tilts away.
        tint = t.mix((1, 1, 1, 1), shade, cover, x=100)
        glass = t.new('ShaderNodeBsdfTransparent', 300, label='Decal (normal only)')
        t.link(tint, glass.inputs['Color'])
        t.link(glass.outputs['BSDF'], out.inputs['Surface'])
        mat.diffuse_color = (1, 1, 1, 0)
    else:
        quadrants = info.get('quadrants') or [[1, 1, 1, 1]] * 4
        colour = quadrant_colour(mat, quadrants, uv, -700, 600)
        if info.get('stitches'):
            colour = t.mix(colour, tuple(info['stitches']), ramp(tex.outputs['Alpha'], .55, .7, -300), x=-100)
        colour = t.mix(colour, shade, 1.0, 'MULTIPLY', x=100)
        normal = t.new('ShaderNodeCombineXYZ', 100)
        for axis, value in zip('XYZ', (nx, ny, nz)):
            t.link(value, normal.inputs[axis])
        packed = t.new('ShaderNodeVectorMath', 200, operation='MULTIPLY_ADD')
        t.link(normal.outputs['Vector'], packed.inputs[0])
        packed.inputs[1].default_value = packed.inputs[2].default_value = (.5, .5, .5)
        nmap = t.new('ShaderNodeNormalMap', 300)
        t.link(packed.outputs['Vector'], nmap.inputs['Color'])
        bsdf = t.new('ShaderNodeBsdfPrincipled', 300, label='Decal')
        t.link(colour, bsdf.inputs['Base Color'])
        t.link(cover, bsdf.inputs['Alpha'])
        t.link(nmap.outputs['Normal'], bsdf.inputs['Normal'])
        bsdf.inputs['Roughness'].default_value = 1.0 - float(info.get('smoothness', .5))
        t.link(bsdf.outputs['BSDF'], out.inputs['Surface'])
        mat.diffuse_color = tuple(quadrants[0])
    for node in t.nodes:
        node.select = False
    tex.select = True
    t.nodes.active = tex
