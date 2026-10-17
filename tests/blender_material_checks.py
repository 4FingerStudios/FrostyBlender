"""Run with Blender --background --factory-startup --python-exit-code 1 --python <this file>."""
import sys
import tempfile
import types
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

    # Test both binding orders: check_existing must not change an earlier use.
    for first_data in (False, True):
        for image in list(bpy.data.images):
            if image.source == 'FILE':
                bpy.data.images.remove(image)
        importer = Importer(assets / 'scene.json', assets)
        first = importer.image('shared.png', first_data)
        second = importer.image('shared.png', not first_data)
        assert first != second
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

print('PASS Blender material checks: diffuse selection, tint, independent color/data images in both binding orders')
