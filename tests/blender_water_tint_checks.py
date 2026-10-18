"""Run in background Blender: render tint outputs and evaluate water animation."""
import sys
import tempfile
import types
from pathlib import Path

import bpy

root = Path(__file__).resolve().parents[1]
package = types.ModuleType('frostbite_importer')
package.__path__ = [str(root/'frostbite_importer')]
sys.modules[package.__name__] = package
from frostbite_importer.materials import build_material
from frostbite_importer.scene_import import Importer

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_plane_add(size=2)
plane = bpy.context.object
camera = bpy.data.cameras.new('Tint check camera')
camera.type, camera.ortho_scale = 'ORTHO', 2
ob = bpy.data.objects.new('Tint check camera', camera)
bpy.context.collection.objects.link(ob)
ob.location.z = 3
scene = bpy.context.scene
scene.camera = ob
scene.render.engine = 'CYCLES'
scene.cycles.samples = 16
scene.cycles.use_denoising = False
scene.cycles.pixel_filter_type = 'BOX'
scene.render.filter_size = .01
scene.render.resolution_x = scene.render.resolution_y = 8
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'OPEN_EXR'
scene.render.image_settings.color_depth = '32'
scene.view_settings.view_transform = 'Standard'
scene.view_settings.look = 'None'

with tempfile.TemporaryDirectory() as tmp:
    assets = Path(tmp)
    importer = Importer(assets/'scene.json', assets)
    for value in (0, 1):
        image = bpy.data.images.new('mask'+str(value), 4, 4, alpha=True)
        image.generated_color = (1, 1, value, 1)
        image.filepath_raw = str(assets/('mask%d.png' % value))
        image.file_format = 'PNG'
        image.save()
        bpy.data.images.remove(image)

    def render_color(doc, label):
        mat = bpy.data.materials.new(label)
        build_material(mat, dict(profile='gw1', **doc), importer.image)
        bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
        output = next(n for n in mat.node_tree.nodes if n.type == 'OUTPUT_MATERIAL')
        emit = mat.node_tree.nodes.new('ShaderNodeEmission')
        socket = bsdf.inputs['Base Color']
        if socket.is_linked:
            mat.node_tree.links.new(socket.links[0].from_socket, emit.inputs['Color'])
        else:
            emit.inputs['Color'].default_value = socket.default_value
        mat.node_tree.links.new(emit.outputs[0], output.inputs['Surface'])
        plane.data.materials.clear()
        plane.data.materials.append(mat)
        scene.render.filepath = str(assets/(label+'.exr'))
        bpy.ops.render.render(write_still=True)
        image = bpy.data.images.load(scene.render.filepath, check_existing=False)
        return list(image.pixels)

    def pixel(pixels, x=4, y=4):
        at = 4*(y*8+x)
        return pixels[at:at+3]

    def close(actual, expected):
        assert all(abs(a-b) < .015 for a, b in zip(actual, expected)), (actual, expected)

    # Source Tint_Colour must override the white field in older exports.
    close(pixel(render_color({'tint': [1, 1, 1, 1], 'vectors': {'Tint_Colour': [.2, .4, .7, 1]}}, 'legacy_tint')), [.2, .4, .7])
    # SSO blue zero leaves the original color intact, rather than black AO.
    for mask, expected in [(0, [1, 1, 1]), (1, [.2, .4, .7])]:
        close(pixel(render_color({'textures': {'specular_masks': 'mask%d.png' % mask},
                                  'vectors': {'Tint_Colour': [.2, .4, .7, 1]}}, 'mask%d' % mask)), expected)
    # The four quadrants in a single render use GW1's Qn_TintColour names.
    for uv in plane.data.uv_layers.active.data:
        uv.uv = (uv.uv.x*2-1, uv.uv.y*2-1)
    colors = [[.8, .1, .1, 1], [.1, .8, .1, 1], [.1, .1, .8, 1], [.8, .8, .1, 1]]
    pixels = render_color({'vectors': {'Q%d_TintColour' % (i+1): c for i, c in enumerate(colors)}}, 'quadrants')
    for (x, y), expected in zip([(6, 6), (1, 6), (6, 1), (1, 1)], colors):
        close(pixel(pixels, x, y), expected)

    mat = bpy.data.materials.new('Water check')
    build_material(mat, {'surface': 'water', 'profile': 'gw1', 'shader_name': 'Ocean_Coastal',
                        'textures': {'normal': 'mask1.png', 'foam_normal': 'mask1.png', 'foam': 'mask1.png'},
                        'source_water': {'AmbientWaveSettings': {'WindSpeed': 8, 'WindAngle': 45}},
                        'native_shader_databases': [{'path': 'shaders/native.shaderdb'}]}, importer.image)
    assert mat['frostbite_game_shader'] == 'water'
    assert mat['frostbite_native_shader_databases'] == 'shaders/native.shaderdb'
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    assert abs(bsdf.inputs['IOR'].default_value-1.333) < .0001
    assert bsdf.inputs['Normal'].is_linked
    assert bsdf.inputs['Transmission Weight'].is_linked
    plane.data.materials.clear()
    plane.data.materials.append(mat)
    scene.render.fps = 24
    time = next(n for n in mat.node_tree.nodes if n.label == 'Water time (seconds)')
    scene.frame_set(1)
    assert time.outputs[0].default_value == 0
    scene.frame_set(25)
    assert time.outputs[0].default_value > 0
    scene.render.filepath = str(assets/'water.exr')
    bpy.ops.render.render(write_still=True)

print('PASS rendered GW1 tint aliases, all UV quadrants, SSO masks, water shader and animated drift')
