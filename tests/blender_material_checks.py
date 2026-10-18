"""Run with Blender --background --factory-startup --python-exit-code 1 --python <this file>."""
import sys
import tempfile
import types
import struct
import zlib
from pathlib import Path

import bpy

root = Path(__file__).resolve().parents[1]
package = types.ModuleType('frostbite_importer')
package.__path__ = [str(root / 'frostbite_importer')]
sys.modules[package.__name__] = package
from frostbite_importer.materials import build_material
from frostbite_importer.scene_import import Importer


with tempfile.TemporaryDirectory() as tmp:
    assets = Path(tmp)
    source = bpy.data.images.new('fixture', 4, 4, alpha=True)
    source.generated_color = (.2, .7, .1, 1)
    source.filepath_raw = str(assets / 'shared.png')
    source.file_format = 'PNG'
    source.save()
    bpy.data.images.remove(source)
    (assets / 'packed.png').write_bytes((assets / 'shared.png').read_bytes())

    # Test both binding orders: check_existing must not change an earlier use.
    for first_data in (False, True):
        for image in list(bpy.data.images):
            if image.source == 'FILE':
                bpy.data.images.remove(image)
        importer = Importer(assets / 'scene.json', assets)
        first = importer.image('shared.png', first_data)
        second = importer.image('shared.png', not first_data)
        assert first != second
        assert first.alpha_mode == second.alpha_mode == 'CHANNEL_PACKED'
        assert first.colorspace_settings.name == ('Non-Color' if first_data else 'sRGB')
        assert second.colorspace_settings.name == ('sRGB' if first_data else 'Non-Color')
        assert importer.image('shared.png', first_data) == first
        assert importer.image('shared.png', not first_data) == second

    tint = [.5, .6, .7, 1]
    doc = {'textures': {'base': 'shared.png', 'normal': 'shared.png', 'mask': 'shared.png'},
           'tint': tint, 'parameters': [tint, [.7, 0, 1, 0], [1, 1, 0, 0]]}
    mat = bpy.data.materials.new('standard')
    build_material(mat, doc, importer.image)
    active = mat.node_tree.nodes.active
    assert active.type == 'TEX_IMAGE' and active.image.colorspace_settings.name == 'sRGB'
    shader = next(n for n in mat.node_tree.nodes if n.type == 'GROUP')
    assert all(abs(a-b) < 1e-6 for a, b in zip(shader.inputs['Tint'].default_value, tint))
    assert any(link.from_node == active and link.to_socket.name == 'Base Color'
               for link in mat.node_tree.links)

    # Both optional shader families must show diffuse, despite their extra images.
    for role in ('asm', 'specular_masks'):
        material = bpy.data.materials.new(role)
        game_doc = {'textures': dict(doc['textures'], **{role: 'shared.png'})}
        build_material(material, game_doc, importer.image, game_shaders=True)
        active = material.node_tree.nodes.active
        assert active.label == 'Color' and active.image.colorspace_settings.name == 'sRGB'
        assert all(n == active or not n.select for n in material.node_tree.nodes)

    # GW1 must use its authored channels even with the optional GW2 shader switch off.
    gw1 = {'profile': 'gw1', 'textures': dict(doc['textures'], psa='packed.png', detail='shared.png'),
           'shader_name': '_pvz/Shaders/Characters/Character_Main_Velvet',
           'vectors': {'Smoothness': [2], 'Specular': [0], 'Detail_UV_Tiling': [25]}}
    material = bpy.data.materials.new('gw1_psa')
    build_material(material, gw1, importer.image)
    assert material['frostbite_game_shader'] == 'gw1'
    bsdf = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    assert bsdf.inputs['Roughness'].is_linked and bsdf.inputs['Specular IOR Level'].is_linked
    assert abs(bsdf.inputs['Sheen Weight'].default_value - .25) < 1e-6
    assert any(n.label == 'Detail' for n in material.node_tree.nodes)
    assert material.node_tree.nodes.active.image.colorspace_settings.name == 'sRGB'

    material = bpy.data.materials.new('gw1_glass')
    build_material(material, {'profile': 'gw1', 'shader_name': 'Character_Main_Glass_Basic',
                             'vectors': {'Opacity': [.7], 'Diffuse': [.2, .1, .9, 1]}}, importer.image)
    bsdf = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    assert bsdf.inputs['Transmission Weight'].default_value == 1
    alpha = bsdf.inputs['Alpha'].links[0].from_node
    assert abs(alpha.inputs[1].default_value - .7) < 1e-6

    material = bpy.data.materials.new('gw1_lights')
    bpy.context.scene.render.fps = 30
    build_material(material, {'profile': 'gw1', 'textures': {'base': 'shared.png', 'light_mask': 'shared.png'},
                             'vectors': {'Lights_Texture_Columns': [2], 'Lights_Texture_Rows': [3],
                                         'Lights_Color_Strength': [900]}}, importer.image)
    bsdf = next(n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    assert bsdf.inputs['Emission Color'].is_linked
    assert bsdf.inputs['Emission Strength'].default_value == 900
    atlas = next(n for n in material.node_tree.nodes if n.label == 'Light atlas frame')
    bpy.data.objects['Cube'].data.materials.clear()
    bpy.data.objects['Cube'].data.materials.append(material)
    bpy.context.scene.frame_set(1)
    assert atlas.outputs[0].default_value == 0
    bpy.context.scene.frame_set(6)
    assert atlas.outputs[0].default_value == 1
    bpy.context.scene.frame_set(31)
    assert atlas.outputs[0].default_value == 0

    # Render RGB hidden behind a zero packed alpha channel. STRAIGHT alpha
    # loses these colours in Blender; checking node links alone misses it.
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind+data) & 0xffffffff)
    pixels = (b'\0' + bytes((128, 64, 32, 0))*4)*4
    (assets / 'zero_alpha.png').write_bytes(b'\x89PNG\r\n\x1a\n' +
        chunk(b'IHDR', struct.pack('>IIBBBBB', 4, 4, 8, 6, 0, 0, 0)) +
        chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))
    packed = importer.image('zero_alpha.png')
    material = bpy.data.materials.new('packed alpha render')
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    tex = nodes.new('ShaderNodeTexImage')
    tex.image = packed
    emit = nodes.new('ShaderNodeEmission')
    out = nodes.new('ShaderNodeOutputMaterial')
    material.node_tree.links.new(tex.outputs['Color'], emit.inputs['Color'])
    material.node_tree.links.new(emit.outputs[0], out.inputs['Surface'])
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    bpy.ops.mesh.primitive_plane_add()
    bpy.context.object.data.materials.append(material)
    camera = bpy.data.cameras.new('packed alpha camera')
    camera.type = 'ORTHO'
    camera.ortho_scale = 2
    ob = bpy.data.objects.new('packed alpha camera', camera)
    bpy.context.collection.objects.link(ob)
    ob.location.z = 3
    scene = bpy.context.scene
    scene.camera = ob
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 1
    scene.render.resolution_x = scene.render.resolution_y = 8
    scene.render.resolution_percentage = 100
    scene.render.filepath = str(assets / 'packed_render.png')
    bpy.ops.render.render(write_still=True)
    rendered = bpy.data.images.load(scene.render.filepath)
    assert min(rendered.pixels[4*(4*8+4):4*(4*8+4)+3]) > .02, 'Packed alpha erased RGB'

print('PASS Blender material checks: diffuse selection, tint, independent color/data images in both binding orders')
