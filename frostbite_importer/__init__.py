"""Frostbite Map Importer: brings GW1 / GW2 / BFN / Skate maps into Blender via aura-plugin-frostbite."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import scene_import

BRIDGE = Path(__file__).with_name('bridge.py')
# A public build carries the import tools, the reader and the BFN type names inside the add-on.
BUNDLED_TOOLS = Path(__file__).with_name('tools')
BUNDLED_READER = Path(__file__).parent / 'bin' / 'aura_frostbite_reader.exe'
BUNDLED_TYPES = Path(__file__).parent / 'data' / 'bfn_types.json'
CREDITS = ('4FingerStudios', 'Made By Brando', 'Plugin made By SillyDev', 'Effects idea by V0e',
           'Shader idea by GalaxyMan2015, Window and Brawltendo')
_job = {}  # the one running background process
# Game executables, by the preference holding that game's folder.
GAMES = {'gw1_path': 'PVZ.Main_Win64_Retail.exe', 'gw2_path': 'GW2.Main_Win64_Retail.exe',
         'bfn_path': 'PVZBattleforNeighborville.exe'}


def game_ok(folder, exe):
    """The game folder holds the game's executable, or for GW1 an Xbox 360 disc copy (Default.xex beside
    Data/Xenon) on its own or in a folder with its title update and DLC packs (tools/gw1_x360.py)."""
    if not folder:
        return False
    folder = Path(bpy.path.abspath(folder))
    if (folder / exe).is_file():
        return True
    return exe == GAMES['gw1_path'] and any((disc / 'Default.xex').is_file() and (disc / 'Data/Xenon').is_dir()
                                            for disc in [folder] + list(folder.glob('*/')))


def default_game(exe):
    """The first install folder holding `exe` under the usual EA app, Origin and Steam locations."""
    roots = [Path('C:/Program Files/EA Games'), Path('C:/Program Files (x86)/Origin Games'),
             Path('C:/Program Files (x86)/Steam/steamapps/common')]
    roots += [Path('%s:/' % d) / sub for d in 'CDEFGHIJKLMNOPQRSTUVWXYZ'
              for sub in ('SteamLibrary/steamapps/common', 'EA Games', 'Origin Games')]
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for folder in root.iterdir():
                if (folder / exe).is_file():
                    return str(folder)
        except OSError:
            continue
    return ''


def _guess(pattern_roots, patterns):
    for root in pattern_roots:
        for pattern in patterns:
            found = sorted(Path(root).glob(pattern)) if Path(root).is_dir() else []
            if found:
                return str(found[0])
    return ''


def default_tools():
    if (BUNDLED_TOOLS / 'gw2.py').is_file():
        return ''  # the bundled tools are used
    return _guess([Path.home() / 'Aura' / 'Plugins'], ['aura-plugin-frostbite/tools'])


def default_reader():
    if BUNDLED_READER.is_file():
        return ''
    return _guess([Path.home() / 'Aura'], ['*/build/bin/RelWithDebInfo/aura_frostbite_reader.exe',
                                           '*/build/bin/*/aura_frostbite_reader.exe'])


def blender_python():
    """Blender's own Python, which has NumPy: the import tools run on it unless another is set."""
    found = Path(sys.executable)
    if found.name.lower().startswith('python') and found.is_file():
        return str(found)
    for candidate in sorted(Path(bpy.app.binary_path).parent.glob('*/python/bin/python*.exe')):
        return str(candidate)
    return ''


class Resolved:
    """The add-on preferences, with empty or missing tool paths replaced by the bundled ones."""
    def __init__(self, real):
        object.__setattr__(self, '_real', real)

    def __getattr__(self, name):
        value = getattr(self._real, name)
        if name == 'python' and not (value and Path(bpy.path.abspath(value)).is_file()):
            return blender_python()
        if name == 'tools' and not (value and (Path(bpy.path.abspath(value)) / 'gw2.py').is_file()):
            return str(BUNDLED_TOOLS) if (BUNDLED_TOOLS / 'gw2.py').is_file() else value
        if name == 'reader' and not (value and Path(bpy.path.abspath(value)).is_file()):
            return str(BUNDLED_READER) if BUNDLED_READER.is_file() else value
        if name == 'bfn_types' and not (value and Path(bpy.path.abspath(value)).is_file()) and BUNDLED_TYPES.is_file():
            return str(BUNDLED_TYPES)
        return value

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


class FrostbitePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    python: StringProperty(name='Python', subtype='FILE_PATH',
                           description="Python 3 with NumPy used to run the import tools. Empty: Blender's own Python")
    tools: StringProperty(name='Tools folder', subtype='DIR_PATH', default=default_tools(),
                          description='aura-plugin-frostbite/tools. Empty: the tools bundled with the add-on')
    reader: StringProperty(name='Reader', subtype='FILE_PATH', default=default_reader(),
                           description='aura_frostbite_reader.exe (decodes textures). Empty: the bundled reader')
    work: StringProperty(name='Output folder', subtype='DIR_PATH',
                         default=str(Path.home() / 'Documents' / 'Frostbite Blender'),
                         description='Converted maps (meshes, textures, scene.json) are kept here')
    bfn_key: StringProperty(name='BFN key', subtype='PASSWORD',
                            description='Battle for Neighborville initfs key: hex (only the first 32 digits are used)')
    bfn_types: StringProperty(name='BFN type names', subtype='FILE_PATH',
                              description='Class and field name table built from a Frosty Editor PVZ3SDK.dll')
    frosty: StringProperty(name='Frosty Editor folder', subtype='DIR_PATH',
                           description='Folder holding FrostySdk.dll and Profiles/PVZ3SDK.dll')
    show_advanced: BoolProperty(name='Advanced', description='Show the tool paths')
    app_link: BoolProperty(name='Link to the FrostyBlender app', default=True, update=lambda self, context: _apply_link(),
                           description='Let the FrostyBlender desktop app send imports to this Blender (this PC only)')
    app_port: IntProperty(name='Port', default=24567, min=1024, max=65535, update=lambda self, context: _apply_link(restart=True),
                          description='Local port the app connects to')
    gw1_path: StringProperty(name='Garden Warfare 1', subtype='DIR_PATH', default=default_game(GAMES['gw1_path']),
                             description='Installed Plants vs. Zombies: Garden Warfare folder (holds PVZ.Main_Win64_Retail.exe)')
    gw2_path: StringProperty(name='Garden Warfare 2', subtype='DIR_PATH', default=default_game(GAMES['gw2_path']),
                             description='Installed Garden Warfare 2 folder (holds GW2.Main_Win64_Retail.exe)')
    bfn_path: StringProperty(name='Battle for Neighborville', subtype='DIR_PATH', default=default_game(GAMES['bfn_path']),
                             description='Installed Battle for Neighborville folder (holds PVZBattleforNeighborville.exe)')

    def draw(self, context):
        self.layout.prop(self, 'work')
        resolved = Resolved(self)
        ready = Path(resolved.python or '-').is_file() and (Path(resolved.tools or '-') / 'gw2.py').is_file()             and Path(resolved.reader or '-').is_file()
        box = self.layout.box()
        header = box.row()
        header.label(text='Import tools: ' + ('ready' if ready else 'not found'), icon='CHECKMARK' if ready else 'ERROR')
        header.prop(self, 'show_advanced', text='Advanced', toggle=True)
        if self.show_advanced or not ready:
            for name in ('python', 'tools', 'reader'):
                box.prop(self, name)
            box.label(text='Leave these empty to use the ones bundled with the add-on', icon='INFO')
        box = self.layout.box()
        box.label(text='Game folders', icon='FILE_FOLDER')
        for name, exe in GAMES.items():
            folder = getattr(self, name)
            ok = game_ok(folder, exe)
            row = box.row(align=True)
            row.prop(self, name)
            row.label(text='', icon='CHECKMARK' if ok else 'ERROR')
        box = self.layout.box()
        row = box.row(align=True)
        row.prop(self, 'app_link')
        row.prop(self, 'app_port')
        if self.app_link:
            box.label(text='App linked' if app_link.running() else 'App link off: port busy (another Blender?)',
                      icon='LINKED' if app_link.running() else 'ERROR')
        box = self.layout.box()
        box.label(text='Battle for Neighborville', icon='WORLD')
        box.prop(self, 'bfn_key')
        if BUNDLED_TYPES.is_file() and not self.show_advanced:
            box.label(text='Type names: bundled', icon='CHECKMARK')
        else:
            box.prop(self, 'bfn_types')
            row = box.row(align=True)
            row.prop(self, 'frosty')
            row.operator('frostbite.build_bfn_types', text='', icon='FILE_REFRESH')
        draw_credits(self.layout)


def draw_credits(layout):
    box = layout.box()
    col = box.column(align=True)
    col.scale_y = 0.8
    for line in CREDITS:
        col.label(text=line)


def prefs(context):
    return Resolved(context.preferences.addons[__package__].preferences)


class FrostbiteMap(bpy.types.PropertyGroup):
    name: StringProperty()
    package: StringProperty()
    mod: StringProperty()


def _sync_game(state, p):
    """A chosen game always uses its current preference folder."""
    name = {'GW1': 'gw1_path', 'GW2': 'gw2_path', 'BFN': 'bfn_path'}.get(state.source)
    if name:
        state.game = getattr(p, name)


def _pick_game(state, context):
    """Choosing a game fills the game folder from the preferences and clears the old map list."""
    name = {'GW1': 'gw1_path', 'GW2': 'gw2_path', 'BFN': 'bfn_path'}.get(state.source)
    if name:
        state.game = getattr(prefs(context), name)
        state.maps.clear()
        state.status = ''


class FrostbiteState(bpy.types.PropertyGroup):
    source: EnumProperty(name='Game', default='CUSTOM', update=_pick_game, items=[
        ('GW1', 'GW1', 'Garden Warfare 1, from the add-on preferences'),
        ('GW2', 'GW2', 'Garden Warfare 2, from the add-on preferences'),
        ('BFN', 'BFN', 'Battle for Neighborville, from the add-on preferences'),
        ('CUSTOM', 'Custom', 'Any other folder (for example a Skate install)')])
    game: StringProperty(name='Game folder', subtype='DIR_PATH',
                         description='Installed PvZ Garden Warfare 1 or 2, Battle for Neighborville (or Skate) folder')
    maps: CollectionProperty(type=FrostbiteMap)
    index: IntProperty()
    texture_size: EnumProperty(name='Texture size', default='1024', items=[
        ('512', '512', ''), ('1024', '1024', ''), ('2048', '2048', 'Largest; uses much more disk')])
    game_shaders: BoolProperty(name='GW2 game shaders', default=False,
                               description='Use GW2 packed masks for occlusion, smoothness, metalness, tint and emission. '
                                           'GW1 materials handle their authored channels automatically')
    keep: BoolProperty(name='Keep intermediate files', default=False,
                       description='Keep raw archive exports and decoded textures (several GB) for debugging')
    include_collision: BoolProperty(name='Collision objects', default=False,
                                    description='Also import the collision copies as hidden wireframes')
    include_markers: BoolProperty(name='Gameplay markers', default=True,
                                  description='Spawns, teleporters, purchase spots and vehicles as labelled empties '
                                              '(a collection that does not render)')
    group_by_level: BoolProperty(name='Group by sub-level', default=True,
                                 description='One collection per streamed sub-level (layers you can hide)')
    status: StringProperty()


def import_scene(context, scene_path, assets=None, state=None):
    assets = assets or scene_import.find_assets(scene_path)
    if not assets:
        raise ValueError('Could not find the assets folder for ' + str(scene_path))
    importer = scene_import.Importer(
        scene_path, assets,
        include_collision=state.include_collision if state else False,
        group_by_level=state.group_by_level if state else True,
        include_markers=state.include_markers if state else True,
        report=lambda level, text: print('Frostbite import:', text))
    importer.run(context)
    return importer


def is_bfn(game):
    return bool(game) and (Path(bpy.path.abspath(game)) / 'PVZBattleforNeighborville.exe').is_file()


def _check_bfn(op, p):
    key = ''.join(p.bfn_key.split())
    if len(key) < 32 or any(c not in '0123456789abcdefABCDEF' for c in key):
        op.report({'ERROR'}, 'Set the BFN key (32+ hex digits) in the add-on preferences')
        return False
    built = Path(bpy.path.abspath(p.work)) / 'bfn_types.json' if p.work else None
    if (not p.bfn_types or not Path(bpy.path.abspath(p.bfn_types)).is_file()) and built and built.is_file():
        # A table built earlier into the output folder.
        p.bfn_types = str(built)
    if not p.bfn_types or not Path(bpy.path.abspath(p.bfn_types)).is_file():
        op.report({'ERROR'}, 'Set the BFN type names file, or build it from a Frosty Editor folder, in the add-on preferences')
        return False
    return True


class FROSTBITE_OT_build_bfn_types(bpy.types.Operator):
    bl_idname = 'frostbite.build_bfn_types'
    bl_label = 'Build BFN type names'
    bl_description = 'Read class and field names from the Frosty Editor PVZ3SDK.dll into a table in the output folder'

    def execute(self, context):
        p = prefs(context)
        frosty = Path(bpy.path.abspath(p.frosty)) if p.frosty else None
        if not frosty or not (frosty / 'Profiles' / 'PVZ3SDK.dll').is_file() or not (frosty / 'FrostySdk.dll').is_file():
            self.report({'ERROR'}, 'Choose the Frosty Editor folder (with FrostySdk.dll and Profiles/PVZ3SDK.dll)')
            return {'CANCELLED'}
        script = Path(bpy.path.abspath(p.tools)) / 'frosty_types.ps1'
        if not script.is_file():
            self.report({'ERROR'}, 'frosty_types.ps1 is missing from the Tools folder')
            return {'CANCELLED'}
        out = Path(bpy.path.abspath(p.work)) / 'bfn_types.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
                               '-Frosty', str(frosty), '-Out', str(out)], capture_output=True, text=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode or not out.is_file():
            self.report({'ERROR'}, 'Building type names failed: ' + ((proc.stderr or proc.stdout).strip().splitlines() or ['no output'])[-1])
            return {'CANCELLED'}
        p.bfn_types = str(out)
        self.report({'INFO'}, 'BFN ' + proc.stdout.strip())
        return {'FINISHED'}


def _check_paths(op, p, need_reader):
    if not p.python or not Path(p.python).is_file():
        op.report({'ERROR'}, 'Set the Python path in the add-on preferences')
        return False
    if not p.tools or not (Path(p.tools) / 'gw2.py').is_file():
        op.report({'ERROR'}, 'Set the Tools folder (aura-plugin-frostbite/tools) in the add-on preferences')
        return False
    if need_reader and (not p.reader or not Path(p.reader).is_file()):
        op.report({'ERROR'}, 'Set the Reader path (aura_frostbite_reader.exe) in the add-on preferences')
        return False
    return True


class FROSTBITE_OT_scan(bpy.types.Operator):
    bl_idname = 'frostbite.scan'
    bl_label = 'Scan maps'
    bl_description = 'List the maps in the chosen game folder'

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite
        _sync_game(state, p)
        if not _check_paths(self, p, False):
            return {'CANCELLED'}
        if not state.game or not Path(bpy.path.abspath(state.game)).is_dir():
            self.report({'ERROR'}, 'Choose the game folder first')
            return {'CANCELLED'}
        out = Path(bpy.app.tempdir or os.environ.get('TEMP', '.')) / 'frostbite_maps.json'
        proc = subprocess.run([p.python, str(BRIDGE), 'discover', '--tools', bpy.path.abspath(p.tools),
                               '--game', bpy.path.abspath(state.game), '--out', str(out)],
                              capture_output=True, text=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if not out.is_file():
            self.report({'ERROR'}, 'Scan failed: ' + (proc.stderr.strip().splitlines() or ['no output'])[-1])
            return {'CANCELLED'}
        result = json.loads(out.read_text(encoding='utf-8'))
        if not result.get('ok'):
            self.report({'ERROR'}, 'Scan failed: ' + result.get('error', ''))
            return {'CANCELLED'}
        state.maps.clear()
        for item in result['maps']:
            entry = state.maps.add()
            entry.name, entry.package, entry.mod = item['name'], item['package'], item.get('mod', '')
        state.index = 0
        self.report({'INFO'}, 'Found %d maps' % len(state.maps))
        return {'FINISHED'}


def _poll_job():
    proc = _job.get('proc')
    if proc is None:
        return None
    job = _job['dir']
    state = bpy.context.scene.frostbite if bpy.context.scene else None
    try:
        progress = json.loads((job / 'progress.json').read_text(encoding='utf-8'))
        message = '%s (%d/3, %ds)' % (progress['message'], progress['step'], time.time() - _job['start'])
    except (OSError, ValueError, KeyError):
        message = 'Starting...'
    if proc.poll() is None:
        if state:
            state.status = message
        _redraw()
        return 1.0
    _job.clear()
    try:
        result = json.loads((job / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = {'ok': False, 'error': 'Import process exited without a result; see logs in ' + str(job)}
    if not result.get('ok'):
        if state:
            state.status = 'Failed: ' + result['error']
        print('Frostbite import failed:', result['error'])
        _redraw()
        return None
    if state:
        state.status = 'Building Blender scene...'
    _redraw()
    try:
        importer = import_scene(bpy.context, result['scene'], Path(result['assets']), state)
        note = ' (%d converter errors, see scene.report.json)' % result['errors'] if result['errors'] else ''
        if state:
            state.status = 'Done: %d meshes, %d materials%s' % (len(importer.meshes), len(importer.materials), note)
    except Exception as error:
        if state:
            state.status = 'Blender import failed: %s' % error
        raise
    finally:
        _redraw()
    return None


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class FROSTBITE_OT_import_map(bpy.types.Operator):
    bl_idname = 'frostbite.import_map'
    bl_label = 'Import map'
    bl_description = 'Convert the selected map in the background, then load it into this scene'

    @classmethod
    def poll(cls, context):
        state = context.scene.frostbite
        return not _job and 0 <= state.index < len(state.maps)

    def execute(self, context):
        p, state = prefs(context), context.scene.frostbite
        _sync_game(state, p)
        game = Path(bpy.path.abspath(state.game))
        if not _check_paths(self, p, True):
            return {'CANCELLED'}
        bfn = is_bfn(state.game)
        if bfn and not _check_bfn(self, p):
            return {'CANCELLED'}
        entry = state.maps[state.index]
        mapname = entry.package.rstrip('/').split('/')[-1]
        job = Path(bpy.path.abspath(p.work)) / mapname
        if job.exists() and (job / 'scene.json').is_file():
            job = job.with_name('%s_%s' % (mapname, time.strftime('%Y%m%d_%H%M%S')))
        job.mkdir(parents=True, exist_ok=True)
        command = [p.python, str(BRIDGE), 'import', '--tools', bpy.path.abspath(p.tools),
                   '--reader', bpy.path.abspath(p.reader), '--game', str(game),
                   '--mod', entry.mod or str(game), '--package', entry.package, '--job', str(job),
                   '--texture-size', state.texture_size]
        if state.keep:
            command.append('--keep')
        env = None
        if bfn:
            command += ['--bfn-types', bpy.path.abspath(p.bfn_types)]
            env = dict(os.environ, AURA_BFN_KEY=''.join(p.bfn_key.split()))
        _job.update(proc=subprocess.Popen(command, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)),
                    dir=job, start=time.time())
        state.status = 'Starting...'
        bpy.app.timers.register(_poll_job, first_interval=1.0)
        self.report({'INFO'}, 'Converting %s into %s' % (entry.name, job))
        return {'FINISHED'}


class FROSTBITE_OT_cancel(bpy.types.Operator):
    bl_idname = 'frostbite.cancel'
    bl_label = 'Cancel'

    @classmethod
    def poll(cls, context):
        return bool(_job)

    def execute(self, context):
        proc = _job.get('proc')
        if proc and os.name == 'nt':
            # /T also stops the converter tool the bridge is currently running.
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        elif proc:
            proc.kill()
        _job.clear()
        context.scene.frostbite.status = 'Cancelled'
        return {'FINISHED'}


class FROSTBITE_OT_open_folder(bpy.types.Operator):
    bl_idname = 'frostbite.open_folder'
    bl_label = 'Open output folder'

    def execute(self, context):
        folder = Path(bpy.path.abspath(prefs(context).work))
        folder.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.path_open(filepath=str(folder))
        return {'FINISHED'}


class FROSTBITE_OT_import_scene(bpy.types.Operator, ImportHelper):
    bl_idname = 'frostbite.import_scene'
    bl_label = 'Import Frostbite scene'
    bl_description = 'Load an already converted scene.json (from this add-on or an Aura editor import job)'
    filename_ext = '.json'
    filter_glob: StringProperty(default='*.json', options={'HIDDEN'})
    assets: StringProperty(name='Assets folder', subtype='DIR_PATH',
                           description='Leave empty to find it next to the scene or in the Aura project')
    include_collision: BoolProperty(name='Collision objects', default=False)
    include_markers: BoolProperty(name='Gameplay markers', default=True)
    group_by_level: BoolProperty(name='Group by sub-level', default=True)

    def execute(self, context):
        assets = Path(self.assets) if self.assets else scene_import.find_assets(self.filepath)
        if not assets:
            self.report({'ERROR'}, 'Could not find the assets folder; set it in the import options')
            return {'CANCELLED'}
        try:
            importer = scene_import.Importer(self.filepath, assets, self.include_collision,
                                             self.group_by_level, report=self.report,
                                             include_markers=self.include_markers)
            importer.run(context)
        except (OSError, ValueError, KeyError) as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class FROSTBITE_UL_maps(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index=0):
        layout.label(text=item.name, icon='WORLD')


class FROSTBITE_PT_panel(bpy.types.Panel):
    bl_label = 'Frostbite Import'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Frostbite'

    def draw(self, context):
        layout, state = self.layout, context.scene.frostbite
        layout.row(align=True).prop(state, 'source', expand=True)
        if state.source == 'CUSTOM':
            layout.prop(state, 'game')
        elif not state.game:
            layout.label(text='Set this game folder in the add-on preferences', icon='ERROR')
        else:
            layout.label(text=state.game, icon='FILE_FOLDER')
        if is_bfn(state.game):
            p = prefs(context)
            if not p.bfn_key.strip() or not p.bfn_types:
                layout.label(text='BFN: set key and type names in preferences', icon='ERROR')
        layout.operator('frostbite.scan', icon='VIEWZOOM')
        layout.template_list('FROSTBITE_UL_maps', '', state, 'maps', state, 'index', rows=6)
        if 0 <= state.index < len(state.maps):
            layout.label(text=state.maps[state.index].package.split('/')[-1], icon='FILE')
        col = layout.column(align=True)
        col.prop(state, 'texture_size')
        col.prop(state, 'game_shaders')
        col.prop(state, 'group_by_level')
        col.prop(state, 'include_collision')
        col.prop(state, 'include_markers')
        col.prop(state, 'keep')
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator('frostbite.import_map', icon='IMPORT')
        if _job:
            row.operator('frostbite.cancel', icon='X', text='')
        if state.status:
            box = layout.box()
            for line in _wrap(state.status, 40):
                box.label(text=line)
        row = layout.row(align=True)
        row.operator('frostbite.import_scene', text='Load scene.json', icon='FILE_FOLDER')
        row.operator('frostbite.open_folder', text='', icon='FILEBROWSER')
        draw_credits(layout)


def _wrap(text, width):
    words, lines, line = text.split(), [], ''
    for word in words:
        if line and len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = (line + ' ' + word).strip()
    return lines + [line] if line else lines


def menu_import(self, context):
    self.layout.operator(FROSTBITE_OT_import_scene.bl_idname, text='Frostbite map scene (.json)')


classes = (FrostbitePreferences, FrostbiteMap, FrostbiteState, FROSTBITE_OT_build_bfn_types, FROSTBITE_OT_scan, FROSTBITE_OT_import_map,
           FROSTBITE_OT_cancel, FROSTBITE_OT_open_folder, FROSTBITE_OT_import_scene, FROSTBITE_UL_maps,
           FROSTBITE_PT_panel)


# The Customizations tab uses the helpers above, so it is imported after them.
from . import animation_import, animations_ui, app_link, cosmetics_ui, effects_ui, npcs_ui, props_ui, rig_tools  # noqa: E402


def _apply_link(restart=False):
    """Start or stop the app link to match the preferences."""
    try:
        p = bpy.context.preferences.addons[__package__].preferences
    except KeyError:
        return 1.0  # preferences not there yet; try again
    if restart or not p.app_link:
        app_link.stop()
    if p.app_link:
        app_link.start(p.app_port)
    return None


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.frostbite = bpy.props.PointerProperty(type=FrostbiteState)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    cosmetics_ui.register()
    npcs_ui.register()
    props_ui.register()
    effects_ui.register()
    rig_tools.register()
    animations_ui.register()
    bpy.app.timers.register(_apply_link, first_interval=1.0)


def unregister():
    app_link.stop()
    animations_ui.unregister()
    rig_tools.unregister()
    effects_ui.unregister()
    props_ui.unregister()
    npcs_ui.unregister()
    cosmetics_ui.unregister()
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    del bpy.types.Scene.frostbite
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
