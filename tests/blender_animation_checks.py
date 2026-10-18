"""Blender background regression: authored translations and missing channels."""
import sys
import types
from pathlib import Path

import bpy
from mathutils import Vector

root = Path(__file__).resolve().parents[1]
package = types.ModuleType('frostbite_importer')
package.__path__ = [str(root / 'frostbite_importer')]
sys.modules[package.__name__] = package
from frostbite_importer.scene_import import AXIS4, BONE_AXES
from frostbite_importer.animation_import import apply_clip

arm = bpy.data.armatures.new('translation fixture')
ob = bpy.data.objects.new('translation fixture', arm)
bpy.context.collection.objects.link(ob)
bpy.context.view_layer.objects.active = ob
ob.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
parent = arm.edit_bones.new('Root')
parent.head, parent.tail = (0, 0, 0), (0, 0, 1)
parent.matrix = AXIS4 @ BONE_AXES
child = arm.edit_bones.new('Attachment')
child.head, child.tail = (0, 0, 1), (0, 0, 2)
child.matrix = AXIS4 @ __import__('mathutils').Matrix.Translation((0, 1, 0)) @ BONE_AXES
child.parent = parent
bpy.ops.object.mode_set(mode='OBJECT')

for additive in (False, True):
    clip = {'name': 'large translations', 'fps': 30, 'additive': additive,
            'frames': [{'Root': {'t': [10, 2, 3]}, 'Attachment': {'t': [0, 5, 0]}},
                       {'Root': {'t': [20, 2, 3]}}]}
    _, matched = apply_clip(bpy.context, ob, clip)
    assert matched == 2
    bpy.context.scene.frame_set(1)
    assert (ob.pose.bones['Root'].matrix.translation-Vector((10, -3, 2))).length < 1e-5
    expected = Vector((10, -3, 8 if additive else 7))
    assert (ob.pose.bones['Attachment'].matrix.translation-expected).length < 1e-5
    bpy.context.scene.frame_set(2)
    assert (ob.pose.bones['Root'].matrix.translation-Vector((20, -3, 2))).length < 1e-5
    # A missing attachment channel returns to its authored rest offset.
    assert (ob.pose.bones['Attachment'].matrix.translation-Vector((20, -3, 3))).length < 1e-5

print('PASS authored root motion, large attachment translations, additive offsets, missing channel rest pose')
