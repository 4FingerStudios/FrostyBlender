"""Blender materials matching Aura's frostbite_pbr shader.

Every material uses the same node layout around one shared node group, so
EEVEE compiles a single shader for the whole map instead of one per material.
"""
import bpy


def placeholder(name, rgba):
    img = bpy.data.images.get(name)
    if img is None:
        # 4x4 rather than 1x1: Blender 5.2 crashes building mipmaps for 1x1 images.
        # generated_color (not edited pixels) is what survives saving the .blend.
        img = bpy.data.images.new(name, 4, 4, alpha=True)
        img.generated_color = rgba
        img.colorspace_settings.name = 'Non-Color'
    return img


def surface_group():
    group = bpy.data.node_groups.get('Frostbite Surface')
    if group:
        return group
    group = bpy.data.node_groups.new('Frostbite Surface', 'ShaderNodeTree')
    for name, kind, default in [
            ('Base Color', 'NodeSocketColor', (1, 1, 1, 1)), ('Base Alpha', 'NodeSocketFloat', 1),
            ('Normal Color', 'NodeSocketColor', (.5, .5, 1, 1)), ('Normal Alpha', 'NodeSocketFloat', .5),
            ('Tint', 'NodeSocketColor', (1, 1, 1, 1)), ('Roughness', 'NodeSocketFloat', .7),
            ('Metallic', 'NodeSocketFloat', 0), ('Alpha Cutoff', 'NodeSocketFloat', -1),
            ('Normal Strength', 'NodeSocketFloat', 1), ('Normal X From Alpha', 'NodeSocketFloat', 0)]:
        group.interface.new_socket(name, in_out='INPUT', socket_type=kind).default_value = default
    group.interface.new_socket('BSDF', in_out='OUTPUT', socket_type='NodeSocketShader')
    n, link = group.nodes, group.links.new
    gi, go = n.new('NodeGroupInput'), n.new('NodeGroupOutput')

    def math(op, a=None, b=None, c=None, clamp=False):
        node = n.new('ShaderNodeMath')
        node.operation = op
        node.use_clamp = clamp
        for i, value in enumerate((a, b, c)):
            if isinstance(value, bpy.types.NodeSocket):
                link(value, node.inputs[i])
            elif value is not None:
                node.inputs[i].default_value = value
        return node.outputs[0]

    tint = n.new('ShaderNodeMix')
    tint.data_type, tint.blend_type = 'RGBA', 'MULTIPLY'
    tint.inputs['Factor'].default_value = 1
    link(gi.outputs['Base Color'], tint.inputs['A'])
    link(gi.outputs['Tint'], tint.inputs['B'])
    alpha = math('GREATER_THAN', gi.outputs['Base Alpha'], gi.outputs['Alpha Cutoff'])

    # Two-channel normal: X from R (or A for _ny maps), Y from G negated because
    # Blender's V points up, Z rebuilt as sqrt(1 - x^2 - y^2) like Aura's shader.
    rgb = n.new('ShaderNodeSeparateColor')
    link(gi.outputs['Normal Color'], rgb.inputs['Color'])
    pick = n.new('ShaderNodeMix')
    pick.data_type = 'FLOAT'
    link(gi.outputs['Normal X From Alpha'], pick.inputs['Factor'])
    link(rgb.outputs['Red'], pick.inputs['A'])
    link(gi.outputs['Normal Alpha'], pick.inputs['B'])
    x = math('MULTIPLY_ADD', pick.outputs['Result'], 2.0, -1.0)
    y = math('MULTIPLY_ADD', rgb.outputs['Green'], -2.0, 1.0)
    z = math('SQRT', math('SUBTRACT', 1.0, math('MULTIPLY_ADD', y, y, math('MULTIPLY', x, x)), clamp=True))
    vec = n.new('ShaderNodeCombineXYZ')
    link(x, vec.inputs['X'])
    link(y, vec.inputs['Y'])
    link(z, vec.inputs['Z'])
    pack = n.new('ShaderNodeVectorMath')
    pack.operation = 'MULTIPLY_ADD'
    pack.inputs[1].default_value = pack.inputs[2].default_value = (.5, .5, .5)
    link(vec.outputs[0], pack.inputs[0])
    nmap = n.new('ShaderNodeNormalMap')
    link(gi.outputs['Normal Strength'], nmap.inputs['Strength'])
    link(pack.outputs[0], nmap.inputs['Color'])

    bsdf = n.new('ShaderNodeBsdfPrincipled')
    link(tint.outputs['Result'], bsdf.inputs['Base Color'])
    link(alpha, bsdf.inputs['Alpha'])
    link(gi.outputs['Roughness'], bsdf.inputs['Roughness'])
    link(gi.outputs['Metallic'], bsdf.inputs['Metallic'])
    link(nmap.outputs['Normal'], bsdf.inputs['Normal'])
    link(bsdf.outputs['BSDF'], go.inputs['BSDF'])
    for i, node in enumerate(n):
        node.location = (i * 160 - 1400, (i % 4) * -180)
    return group


def build_material(mat, doc, image, game_shaders=False):
    if game_shaders:
        from .game_materials import build as build_game
        if build_game(mat, doc, image):
            return
    params = doc.get('parameters') or [[1, 1, 1, 1], [.7, 0, 1, 0], [1, 1, 0, 0], [0, 0, 0, 0]]
    tint = [a * b for a, b in zip(params[0], doc.get('tint', [1, 1, 1, 1]))]
    surface, tiling = params[1], params[2]
    textures = doc.get('textures', {})
    cutoff = doc.get('alpha_cutoff', -1)
    cutoff = -1.0 if cutoff is None or cutoff < 0 else float(cutoff)
    mat.use_nodes = True
    mat.use_backface_culling = not doc.get('double_sided', False)
    mat.diffuse_color = (tint[0], tint[1], tint[2], 1)
    nodes, link = mat.node_tree.nodes, mat.node_tree.links.new
    nodes.clear()

    uvmap = nodes.new('ShaderNodeUVMap')
    uvmap.location = (-900, 0)
    # Aura applies uv*scale+offset with V down; this is the same transform with V up.
    tile = nodes.new('ShaderNodeVectorMath')
    tile.operation = 'MULTIPLY_ADD'
    tile.location = (-700, 0)
    tile.inputs[1].default_value = (tiling[0], tiling[1], 1)
    tile.inputs[2].default_value = (tiling[2], 1 - tiling[1] - tiling[3], 0)
    link(uvmap.outputs['UV'], tile.inputs[0])

    base = nodes.new('ShaderNodeTexImage')
    base.image = image(textures.get('base'), False) or placeholder('frostbite_white', (1, 1, 1, 1))
    base.location = (-450, 200)
    normal_image = image(textures.get('normal'), True) if float(surface[2]) > .5 else None
    normal = nodes.new('ShaderNodeTexImage')
    normal.image = normal_image or placeholder('frostbite_flat_normal', (.5, .5, 1, .5))
    normal.location = (-450, -150)
    for tex in (base, normal):
        link(tile.outputs['Vector'], tex.inputs['Vector'])

    group = nodes.new('ShaderNodeGroup')
    group.node_tree = surface_group()
    group.location = (-100, 0)
    gin = group.inputs
    link(base.outputs['Color'], gin['Base Color'])
    link(base.outputs['Alpha'], gin['Base Alpha'])
    link(normal.outputs['Color'], gin['Normal Color'])
    link(normal.outputs['Alpha'], gin['Normal Alpha'])
    gin['Tint'].default_value = (tint[0], tint[1], tint[2], 1)
    gin['Roughness'].default_value = float(surface[0])
    gin['Metallic'].default_value = float(surface[1])
    gin['Alpha Cutoff'].default_value = cutoff
    gin['Normal Strength'].default_value = 1.0 if normal_image else 0.0
    gin['Normal X From Alpha'].default_value = 1.0 if float(surface[3]) > .5 else 0.0

    out = nodes.new('ShaderNodeOutputMaterial')
    out.location = (150, 0)
    link(group.outputs['BSDF'], out.inputs['Surface'])
    if textures.get('mask'):
        mask = nodes.new('ShaderNodeTexImage')
        mask.image = image(textures['mask'], True)
        mask.label = 'Mask (unused by Aura shader)'
        mask.location = (-450, -500)
