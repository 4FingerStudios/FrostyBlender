"""Animated Blender approximation of exported Frostbite water settings."""
import math

import bpy

from .game_materials import _Nodes


def build(mat, doc, image):
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    mat.use_backface_culling = False
    mat['frostbite_game_shader'] = 'water'
    mat['frostbite_source_shader'] = doc.get('shader_name', '')
    mat['frostbite_surface_approximation'] = doc.get('surface_approximation', '')
    mat['frostbite_native_shader_databases'] = '\n'.join(s['path'] for s in doc.get('native_shader_databases', []))
    if hasattr(mat, 'surface_render_method'):
        mat.surface_render_method = 'DITHERED'
    if hasattr(mat, 'use_screen_refraction'):
        mat.use_screen_refraction = True
    t = _Nodes(mat)
    source = doc.get('source_water', {})
    ambient = source.get('AmbientWaveSettings', {})
    detail = source.get('DetailWaveSettings', {})
    color = tuple(doc.get('water_color', [.04, .18, .24, 1]))
    mat.diffuse_color = color
    bsdf = t.new('ShaderNodeBsdfPrincipled', 600, label='Water refraction')
    bsdf.inputs['Base Color'].default_value = color
    bsdf.inputs['Metallic'].default_value = 0
    bsdf.inputs['Roughness'].default_value = .12
    bsdf.inputs['IOR'].default_value = 1.333
    bsdf.inputs['Transmission Weight'].default_value = 1
    out = t.new('ShaderNodeOutputMaterial', 1000)
    t.link(bsdf.outputs['BSDF'], out.inputs['Surface'])

    geometry = t.new('ShaderNodeNewGeometry', -1600)
    xy = t.new('ShaderNodeSeparateXYZ', -1500)
    t.link(geometry.outputs['Position'], xy.inputs[0])
    planar = t.new('ShaderNodeCombineXYZ', -1400, label='World water coordinates')
    t.link(xy.outputs['X'], planar.inputs['X'])
    t.link(xy.outputs['Y'], planar.inputs['Y'])
    seconds = t.new('ShaderNodeValue', -1600, label='Water time (seconds)')
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
    seconds.outputs[0].driver_add('default_value').driver.expression = '(frame-1)/%r' % fps

    def coordinates(settings, dimension, reverse=False):
        angle = math.radians(settings.get('WindAngle', 45))
        # Authored wind controls direction and relative drift, not a recreation
        # of the game's FFT spectrum. World-space sampling avoids giant UVs.
        speed = settings.get('WindSpeed', 8) * .015 * (-1 if reverse else 1)
        offset = t.new('ShaderNodeCombineXYZ', -1300, label='Wind drift')
        t.link(t.math('MULTIPLY', seconds.outputs[0], math.cos(angle)*speed), offset.inputs['X'])
        t.link(t.math('MULTIPLY', seconds.outputs[0], math.sin(angle)*speed), offset.inputs['Y'])
        uv = t.new('ShaderNodeVectorMath', -1100, operation='MULTIPLY_ADD')
        t.link(planar.outputs[0], uv.inputs[0])
        uv.inputs[1].default_value = (1/max(1, dimension),)*3
        t.link(offset.outputs[0], uv.inputs[2])
        return uv.outputs[0]

    def texture(role, coords):
        path = doc.get('textures', {}).get(role)
        img = image(path, True) if path else None
        if img is None:
            return None
        node = t.new('ShaderNodeTexImage', -800, label='Water '+role, image=img, extension='REPEAT')
        t.link(coords, node.inputs['Vector'])
        return node

    main_uv = coordinates(ambient, source.get('TileDimension', 64))
    fine_uv = coordinates(detail, source.get('DetailTileDimension', 64)*.25, True)
    previous = None
    for role, coords, settings in [('normal', main_uv, ambient), ('foam_normal', fine_uv, detail)]:
        node = texture(role, coords)
        if not node:
            continue
        # GW1 textures are DirectX normals. Flip green before Normal Map.
        split = t.split(node)
        pack = t.new('ShaderNodeCombineColor', -200)
        t.link(split.outputs['Red'], pack.inputs['Red'])
        t.link(t.math('SUBTRACT', 1, split.outputs['Green']), pack.inputs['Green'])
        t.link(split.outputs['Blue'], pack.inputs['Blue'])
        normal = t.new('ShaderNodeNormalMap', 0, label='Water '+role)
        normal.space = 'WORLD'
        normal.inputs['Strength'].default_value = min(1, max(0, settings.get('WaveAmplitude', .2)*source.get('WaveAmplitudeScale', 1)))
        t.link(pack.outputs[0], normal.inputs['Color'])
        if previous is None:
            previous = normal.outputs[0]
        else:
            combine = t.new('ShaderNodeVectorMath', 200, operation='ADD')
            t.link(previous, combine.inputs[0])
            t.link(normal.outputs[0], combine.inputs[1])
            unit = t.new('ShaderNodeVectorMath', 400, operation='NORMALIZE')
            t.link(combine.outputs[0], unit.inputs[0])
            previous = unit.outputs[0]
    if previous is not None:
        t.link(previous, bsdf.inputs['Normal'])
    foam = texture('foam', fine_uv)
    if foam:
        mask = t.math('MULTIPLY', t.math('GREATER_THAN', t.split(foam).outputs['Red'], .65), .3)
        t.set(bsdf.inputs['Base Color'], t.mix(color, (.8, .9, .9, 1), mask))
        t.set(bsdf.inputs['Roughness'], t.math('ADD', .12, t.math('MULTIPLY', mask, .6)))
        t.set(bsdf.inputs['Transmission Weight'], t.math('SUBTRACT', 1, mask))
    for node in t.nodes:
        node.select = False
