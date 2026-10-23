"""Props tab: browse a game's maps and their props, and import the chosen ones.

Props are laid out in a row along X, resting on the ground, each in its own collection.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty

from . import BRIDGE, GAMES, game_ok, _check_bfn, _check_paths, _redraw, _wrap, draw_credits, prefs, scene_import

SOURCES = {'GW1': 'gw1_path', 'GW2': 'gw2_path', 'BFN': 'bfn_path'}
_data = {}    # game folder -> discover result
_job = {}     # the running props import
_groups = []  # EnumProperty items must stay referenced while Blender shows them


def _game(context):
    return getattr(prefs(context), SOURCES[context.scene.frostbite_props.source], '') or ''


def _map(context):
    state = context.scene.frostbite_props
    data = _data.get(_game(context))
    if not data or not 0 <= state.map_index < len(state.maps):
        return None
    return next((m for m in data['maps'] if m['package'] == state.maps[state.map_index].package), None)


class FrostbitePropMap(bpy.types.PropertyGroup):
    name: StringProperty()
    package: StringProperty()
    count: IntProperty()


class FrostbiteProp(bpy.types.PropertyGroup):
    name: StringProperty()
    group: StringProperty()
    asset: StringProperty()
    bundle: StringProperty()
    selected: BoolProperty(name='', description='Include this prop in the import')


def _fill_props(state, context):
    state.props.clear()
    state.prop_index = 0
    found = _map(context)
    for prop in (found or {}).get('props', []):
        entry = state.props.add()
        entry.name, entry.group, entry.asset, entry.bundle = prop['name'], prop['group'], prop['asset'], prop['bundle']
    state.group = 'ALL'


def _group_items(state, context):
    _groups[:] = [('ALL', 'All groups', '')] + [(g, g, '') for g in sorted({p.group for p in state.props})]
    return _groups


def _reset(state, context):
    state.maps.clear()
    state.props.clear()
    state.status = ''


class FrostbitePropsState(bpy.types.PropertyGroup):
    source: EnumProperty(name='Game', default='GW2', update=_reset, items=[
        ('GW1', 'GW1', 'Garden Warfare 1'), ('GW2', 'GW2', 'Garden Warfare 2'), ('BFN', 'BFN', 'Battle for Neighborville')])
    maps: CollectionProperty(type=FrostbitePropMap)
    map_index: IntProperty(update=_fill_props)
    props: CollectionProperty(type=FrostbiteProp)
    prop_index: IntProperty()
    group: EnumProperty(name='Group', items=_group_items)
    search: StringProperty(name='', description='Filter props by name', options={'TEXTEDIT_UPDATE'})
    status: StringProperty()


class FROSTBITE_OT_props_scan(bpy.types.Operator):
    bl_idname = 'frostbite.props_scan'
    bl_label = 'Scan maps'
    bl_description = "List the chosen game's maps and the props in each"

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_props
        if not _check_paths(self, p, False):
            return {'CANCELLED'}
        game = _game(context)
        if not game_ok(game, GAMES[SOURCES[state.source]]):
            self.report({'ERROR'}, 'Set the %s game folder in the add-on preferences' % state.source)
            return {'CANCELLED'}
        out = Path(bpy.app.tempdir or os.environ.get('TEMP', '.')) / 'frostbite_props.json'
        proc = subprocess.run([p.python, str(BRIDGE), 'props-discover', '--tools', bpy.path.abspath(p.tools),
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
        state.maps.clear()
        for found in result['maps']:
            entry = state.maps.add()
            entry.name, entry.package, entry.count = found['name'], found['package'], len(found['props'])
        state.map_index = 0
        _fill_props(state, context)
        self.report({'INFO'}, 'Found %d maps, %d props' % (len(state.maps), sum(m.count for m in state.maps)))
        return {'FINISHED'}


class FROSTBITE_OT_props_clear(bpy.types.Operator):
    bl_idname = 'frostbite.props_clear'
    bl_label = 'Clear selection'
    bl_description = 'Untick every prop of this map'

    def execute(self, context):
        for prop in context.scene.frostbite_props.props:
            prop.selected = False
        return {'FINISHED'}


def _poll():
    proc = _job.get('proc')
    if proc is None:
        return None
    state = bpy.context.scene.frostbite_props if bpy.context.scene else None
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
    label = _job['label']
    _job.clear()
    try:
        result = json.loads((job / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = {'ok': False, 'error': 'Import process exited without a result; see logs in ' + str(job)}
    try:
        if not result.get('ok'):
            raise RuntimeError(result['error'])
        importer = scene_import.Importer(result['scene'], Path(result['assets']), False, True,
                                         report=lambda level, text: print('Frostbite props:', text))
        root = importer.run(bpy.context)
        if root is not None:
            root.name = label
        note = ' (%d converter errors)' % result['errors'] if result.get('errors') else ''
        if state:
            state.status = 'Done: %d props%s' % (len(importer.meshes), note)
    except Exception as error:
        if state:
            state.status = 'Failed: %s' % error
        print('Frostbite props import failed:', error)
    _redraw()
    return None


class FROSTBITE_OT_props_import(bpy.types.Operator):
    bl_idname = 'frostbite.props_import'
    bl_label = 'Import selected'
    bl_description = 'Convert the ticked props (or the highlighted one) in the background and add them to this scene'

    @classmethod
    def poll(cls, context):
        return not _job and len(context.scene.frostbite_props.props) > 0

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite_props
        if not _check_paths(self, p, True):
            return {'CANCELLED'}
        bfn = state.source == 'BFN'
        if bfn and not _check_bfn(self, p):
            return {'CANCELLED'}
        chosen = [i for i in state.props if i.selected]
        if not chosen and 0 <= state.prop_index < len(state.props):
            chosen = [state.props[state.prop_index]]
        found = _map(context)
        if not chosen or not found:
            self.report({'ERROR'}, 'Tick the props to import')
            return {'CANCELLED'}
        stamp = time.strftime('%Y%m%d_%H%M%S')
        leaf = found['package'].rstrip('/').split('/')[-1].lower()
        job = Path(bpy.path.abspath(p.work)) / 'props' / ('%s_%s_%s' % (state.source.lower(), leaf, stamp))
        job.mkdir(parents=True, exist_ok=True)
        request = {'props': [{'name': i.name, 'group': i.group, 'asset': i.asset, 'bundle': i.bundle} for i in chosen],
                   'variation_dbs': found.get('variation_dbs', [])}
        (job / 'items.json').write_text(json.dumps(request, indent=1), encoding='utf-8')
        command = [p.python, str(BRIDGE), 'props-import', '--tools', bpy.path.abspath(p.tools),
                   '--reader', bpy.path.abspath(p.reader), '--game', bpy.path.abspath(_game(context)),
                   '--items', str(job / 'items.json'), '--job', str(job),
                   '--texture-size', context.scene.frostbite.texture_size]
        env = None
        if bfn:
            command += ['--bfn-types', bpy.path.abspath(p.bfn_types)]
            env = dict(os.environ, AURA_BFN_KEY=''.join(p.bfn_key.split()))
        _job.update(proc=subprocess.Popen(command, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
                    dir=job, start=time.time(), label='%s %s props' % (state.source, found['name']))
        state.status = 'Starting...'
        bpy.app.timers.register(_poll, first_interval=1.0)
        self.report({'INFO'}, 'Converting %d props into %s' % (len(chosen), job))
        return {'FINISHED'}


class FROSTBITE_UL_prop_maps(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row()
        row.label(text=item.name, icon='WORLD')
        row.label(text=str(item.count))


class FROSTBITE_UL_props(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        row = layout.row(align=True)
        row.prop(item, 'selected')
        row.label(text=item.name)

    def filter_items(self, context, data, propname):
        state = context.scene.frostbite_props
        search = state.search.lower().replace(' ', '')
        flags = [self.bitflag_filter_item if (state.group in ('ALL', '', i.group) and
                                              (not search or search in i.name.lower().replace(' ', ''))) else 0
                 for i in getattr(data, propname)]
        return flags, []


class FROSTBITE_PT_props(bpy.types.Panel):
    bl_label = 'Frostbite Props'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Props'

    def draw(self, context):
        layout, state = self.layout, context.scene.frostbite_props
        layout.row(align=True).prop(state, 'source', expand=True)
        if not _game(context):
            layout.label(text='Set this game folder in the add-on preferences', icon='ERROR')
        layout.operator('frostbite.props_scan', icon='VIEWZOOM')
        layout.label(text='Map')
        layout.template_list('FROSTBITE_UL_prop_maps', '', state, 'maps', state, 'map_index', rows=5)
        if state.props:
            row = layout.row(align=True)
            row.prop(state, 'group', text='')
            row.prop(state, 'search', icon='VIEWZOOM')
            layout.template_list('FROSTBITE_UL_props', '', state, 'props', state, 'prop_index', rows=10)
            ticked = sum(1 for i in state.props if i.selected)
            row = layout.row(align=True)
            row.label(text='%d ticked' % ticked if ticked else 'Tick props, or import the highlighted one')
            row.operator('frostbite.props_clear', text='', icon='X')
        layout.prop(context.scene.frostbite, 'texture_size')
        layout.prop(context.scene.frostbite, 'game_shaders')
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator('frostbite.props_import', icon='IMPORT')
        if state.status:
            box = layout.box()
            for line in _wrap(state.status, 40):
                box.label(text=line)
        draw_credits(layout)


classes = (FrostbitePropMap, FrostbiteProp, FrostbitePropsState, FROSTBITE_OT_props_scan, FROSTBITE_OT_props_clear,
           FROSTBITE_OT_props_import, FROSTBITE_UL_prop_maps, FROSTBITE_UL_props, FROSTBITE_PT_props)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frostbite_props = bpy.props.PointerProperty(type=FrostbitePropsState)


def unregister():
    del bpy.types.Scene.frostbite_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
