"""Effects tab: browse a game's visual effects by group and import the chosen ones.

Each effect arrives as an empty holding one Geometry Nodes particle object per emitter, plus its
lights, placed at the 3D cursor (several in a row). Press play: particles spawn from frame 1, or
from the time set as "Start (s)" on each emitter's modifier.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from mathutils import Vector

from . import BRIDGE, GAMES, _check_paths, _redraw, _wrap, draw_credits, prefs, scene_import

SOURCES = {'GW1': 'gw1_path', 'GW2': 'gw2_path'}
_data = {}    # game folder -> discover result
_job = {}     # the running effects import


def _game(context):
    return getattr(prefs(context), SOURCES[context.scene.frostbite_effects.source], '') or ''


def _group(context):
    state = context.scene.frostbite_effects
    data = _data.get(_game(context))
    if not data or not 0 <= state.group_index < len(state.groups):
        return None
    return next((g for g in data['groups'] if g['name'] == state.groups[state.group_index].name), None)


class FrostbiteEffectGroup(bpy.types.PropertyGroup):
    name: StringProperty()
    count: IntProperty()


class FrostbiteEffect(bpy.types.PropertyGroup):
    name: StringProperty()
    group: StringProperty()
    asset: StringProperty()
    bundle: StringProperty()
    selected: BoolProperty(name='', description='Include this effect in the import')


def _fill_effects(state, context):
    state.effects.clear()
    state.effect_index = 0
    for effect in (_group(context) or {}).get('effects', []):
        entry = state.effects.add()
        entry.name, entry.group, entry.asset, entry.bundle = effect['name'], effect['group'], effect['asset'], effect['bundle']


def _reset(state, context):
    state.groups.clear()
    state.effects.clear()
    state.status = ''


class FrostbiteEffectsState(bpy.types.PropertyGroup):
    source: EnumProperty(name='Game', default='GW2', update=_reset, items=[
        ('GW1', 'GW1', 'Garden Warfare 1'), ('GW2', 'GW2', 'Garden Warfare 2')])
    groups: CollectionProperty(type=FrostbiteEffectGroup)
    group_index: IntProperty(update=_fill_effects)
    effects: CollectionProperty(type=FrostbiteEffect)
    effect_index: IntProperty()
    search: StringProperty(name='', description='Filter effects by name', options={'TEXTEDIT_UPDATE'})
    group_search: StringProperty(name='', description='Filter groups by name', options={'TEXTEDIT_UPDATE'})
    at_cursor: BoolProperty(name='Place at 3D cursor', default=True,
                            description='Put the imported effects at the 3D cursor instead of the world origin')
    status: StringProperty()


class FROSTBITE_OT_effects_scan(bpy.types.Operator):
    bl_idname = 'frostbite.effects_scan'
    bl_label = 'Scan effects'
    bl_description = "List the chosen game's visual effects"

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_effects
        if not _check_paths(self, p, False):
            return {'CANCELLED'}
        game = _game(context)
        if not game or not (Path(bpy.path.abspath(game)) / GAMES[SOURCES[state.source]]).is_file():
            self.report({'ERROR'}, 'Set the %s game folder in the add-on preferences' % state.source)
            return {'CANCELLED'}
        out = Path(bpy.app.tempdir or os.environ.get('TEMP', '.')) / 'frostbite_effects.json'
        proc = subprocess.run([p.python, str(BRIDGE), 'effects-discover', '--tools', bpy.path.abspath(p.tools),
                               '--game', bpy.path.abspath(game), '--out', str(out)], capture_output=True, text=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            result = json.loads(out.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.report({'ERROR'}, 'Scan failed: ' + ((proc.stderr or '').strip().splitlines() or ['no output'])[-1])
            return {'CANCELLED'}
        if not result.get('ok'):
            self.report({'ERROR'}, 'Scan failed: ' + result.get('error', ''))
            return {'CANCELLED'}
        _data[game] = result
        state.groups.clear()
        for found in result['groups']:
            entry = state.groups.add()
            entry.name, entry.count = found['name'], len(found['effects'])
        state.group_index = 0
        _fill_effects(state, context)
        self.report({'INFO'}, 'Found %d effects in %d groups' % (sum(g.count for g in state.groups), len(state.groups)))
        return {'FINISHED'}


class FROSTBITE_OT_effects_clear(bpy.types.Operator):
    bl_idname = 'frostbite.effects_clear'
    bl_label = 'Clear selection'
    bl_description = 'Untick every effect of this group'

    def execute(self, context):
        for effect in context.scene.frostbite_effects.effects:
            effect.selected = False
        return {'FINISHED'}


def _poll():
    proc = _job.get('proc')
    if proc is None:
        return None
    state = bpy.context.scene.frostbite_effects if bpy.context.scene else None
    job = _job['dir']
    if proc.poll() is None:
        try:
            progress = json.loads((job / 'progress.json').read_text(encoding='utf-8'))
            message = '%s (%d/3, %ds)' % (progress['message'], progress['step'], time.time() - _job['start'])
        except (OSError, ValueError, KeyError):
            message = 'Starting...'
        if state:
            state.status = message
        _redraw()
        return 1.0
    label, cursor = _job['label'], _job['cursor']
    _job.clear()
    try:
        result = json.loads((job / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = {'ok': False, 'error': 'Import process exited without a result; see logs in ' + str(job)}
    try:
        if not result.get('ok'):
            raise RuntimeError(result['error'])
        importer = scene_import.Importer(result['scene'], Path(result['assets']), False, True, add_lighting=False,
                                         report=lambda level, text: print('Frostbite effects:', text))
        root = importer.run(bpy.context)
        holders = []
        if root is not None:
            root.name = label
            holders = [o for o in root.all_objects if o.get('frostbite_effect') is not None and o.parent is None]
            for holder in holders:
                holder.location = holder.location + cursor
        note = ' (%d skipped)' % result['errors'] if result.get('errors') else ''
        if importer.warnings:
            note += ', %d warnings (see console)' % len(importer.warnings)
        if state:
            state.status = 'Done: %d effects%s. Press play to see them.' % (len(holders), note)
    except Exception as error:
        if state:
            state.status = 'Failed: %s' % error
        print('Frostbite effects import failed:', error)
    _redraw()
    return None


class FROSTBITE_OT_effects_import(bpy.types.Operator):
    bl_idname = 'frostbite.effects_import'
    bl_label = 'Import selected'
    bl_description = 'Convert the ticked effects (or the highlighted one) in the background and add them to this scene'

    @classmethod
    def poll(cls, context):
        return not _job and len(context.scene.frostbite_effects.effects) > 0

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_effects
        if not _check_paths(self, p, True):
            return {'CANCELLED'}
        chosen = [i for i in state.effects if i.selected]
        if not chosen and 0 <= state.effect_index < len(state.effects):
            chosen = [state.effects[state.effect_index]]
        if not chosen:
            self.report({'ERROR'}, 'Tick the effects to import')
            return {'CANCELLED'}
        stamp = time.strftime('%Y%m%d_%H%M%S')
        job = Path(bpy.path.abspath(p.work)) / 'effects' / ('%s_%s' % (state.source.lower(), stamp))
        job.mkdir(parents=True, exist_ok=True)
        items = [{'name': i.name, 'group': i.group, 'asset': i.asset, 'bundle': i.bundle} for i in chosen]
        (job / 'items.json').write_text(json.dumps(items, indent=1), encoding='utf-8')
        command = [p.python, str(BRIDGE), 'effects-import', '--tools', bpy.path.abspath(p.tools),
                   '--reader', bpy.path.abspath(p.reader), '--game', bpy.path.abspath(_game(context)),
                   '--items', str(job / 'items.json'), '--job', str(job),
                   '--texture-size', context.scene.frostbite.texture_size]
        cursor = context.scene.cursor.location.copy() if state.at_cursor else Vector()
        label = chosen[0].name if len(chosen) == 1 else '%s effects' % state.source
        _job.update(proc=subprocess.Popen(command, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
                    dir=job, start=time.time(), label=label, cursor=cursor)
        state.status = 'Starting...'
        bpy.app.timers.register(_poll, first_interval=1.0)
        self.report({'INFO'}, 'Converting %d effects into %s' % (len(chosen), job))
        return {'FINISHED'}


class FROSTBITE_UL_effect_groups(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row()
        row.label(text=item.name, icon='SHADERFX')
        row.label(text=str(item.count))

    def filter_items(self, context, data, propname):
        search = context.scene.frostbite_effects.group_search.lower().replace(' ', '')
        return [self.bitflag_filter_item if not search or search in i.name.lower().replace(' ', '') else 0
                for i in getattr(data, propname)], []


class FROSTBITE_UL_effects(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row(align=True)
        row.prop(item, 'selected')
        row.label(text=item.name)

    def filter_items(self, context, data, propname):
        search = context.scene.frostbite_effects.search.lower().replace(' ', '')
        return [self.bitflag_filter_item if not search or search in i.name.lower().replace(' ', '') else 0
                for i in getattr(data, propname)], []


class FROSTBITE_PT_effects(bpy.types.Panel):
    bl_label = 'Frostbite Effects'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Effects'

    def draw(self, context):
        layout, state = self.layout, context.scene.frostbite_effects
        layout.row(align=True).prop(state, 'source', expand=True)
        if not _game(context):
            layout.label(text='Set this game folder in the add-on preferences', icon='ERROR')
        layout.operator('frostbite.effects_scan', icon='VIEWZOOM')
        if state.groups:
            layout.label(text='Group')
            layout.prop(state, 'group_search', icon='VIEWZOOM')
            layout.template_list('FROSTBITE_UL_effect_groups', '', state, 'groups', state, 'group_index', rows=6)
        if state.effects:
            layout.prop(state, 'search', icon='VIEWZOOM')
            layout.template_list('FROSTBITE_UL_effects', '', state, 'effects', state, 'effect_index', rows=8)
            ticked = sum(1 for i in state.effects if i.selected)
            row = layout.row(align=True)
            row.label(text='%d ticked' % ticked if ticked else 'Tick effects, or import the highlighted one')
            row.operator('frostbite.effects_clear', text='', icon='X')
        layout.prop(state, 'at_cursor')
        layout.prop(context.scene.frostbite, 'texture_size')
        layout.prop(context.scene.frostbite, 'game_shaders')
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator('frostbite.effects_import', icon='IMPORT')
        if state.status:
            box = layout.box()
            for line in _wrap(state.status, 40):
                box.label(text=line)
        if not context.scene.camera:
            layout.label(text='Add a camera: quads turn to face it', icon='INFO')
        draw_credits(layout)


classes = (FrostbiteEffectGroup, FrostbiteEffect, FrostbiteEffectsState, FROSTBITE_OT_effects_scan,
           FROSTBITE_OT_effects_clear, FROSTBITE_OT_effects_import, FROSTBITE_UL_effect_groups, FROSTBITE_UL_effects,
           FROSTBITE_PT_effects)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frostbite_effects = bpy.props.PointerProperty(type=FrostbiteEffectsState)


def unregister():
    del bpy.types.Scene.frostbite_effects
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
