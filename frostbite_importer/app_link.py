"""Link to the FrostyBlender desktop app.

While Blender runs, the add-on listens on 127.0.0.1 only. The app browses the games itself
(it runs the same discover tools on Blender's Python, found through link.json) and sends the
chosen items here; each request fills the matching sidebar tab and runs its Import button, so
an import from the app behaves exactly like one from the tab.

    GET  /ping        add-on version, Blender version
    GET  /status      each tab's status line and whether an import is running
    GET  /armatures   armatures in the scene (animation targets), active one first
    POST /import      {"kind": "maps|cosmetics|npcs|props|effects|animations", "game": "GW1|GW2|BFN", ...}
                      with "options": {"unreal": true} (maps, cosmetics, npcs, props), what the import added
                      is then sent on to the open Unreal Editor (unreal_export.send_to_unreal)
    POST /addon-update {"archive": "...zip", "digest": "sha256:...", "version": "..."}

Requests must carry the X-FrostyBlender header and no Origin header, so a web page in a
browser cannot drive Blender. link.json (in %LOCALAPPDATA%/FrostyBlender) tells the app the
port and where Blender's Python, the tools and the game folders are; it never holds the BFN key.
"""
import errno
import json
import os
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy

DEFAULT_PORT = 24567
PORT_TRIES = 64                # the link's own port and the ones after it, past reserved ranges
_server = None
_requests = queue.Queue()   # (job id, request) for the main thread
_jobs = {}                  # job id -> {'state', 'message', 'kind'}
_unreal = {}                # job id -> {'before': object names, 'name'} for imports sent on to Unreal
UNREAL_KINDS = ('maps', 'cosmetics', 'npcs', 'props')
_counter = [0]
_last_link = [0.0]


def link_dir():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'FrostyBlender'


def _read_version():
    try:
        manifest = Path(__file__).with_name('blender_manifest.toml').read_text(encoding='utf-8')
        return next(line.split('"')[1] for line in manifest.splitlines() if line.startswith('version'))
    except (OSError, StopIteration, IndexError):
        return ''


_loaded_version = _read_version()
_restart_required = None


def _version():
    return _loaded_version  # Files on disk can be newer than the code running until restart.


def write_link(port):
    """Where the app finds this Blender's Python, the tools and the game folders."""
    from . import BRIDGE, prefs
    try:
        p = prefs(bpy.context)
    except (KeyError, AttributeError):
        return
    data = {'format': 'frostyblender-link', 'version': 1, 'addon_version': _version(), 'port': port,
            'pid': os.getpid(), 'blender': bpy.app.binary_path, 'blender_version': bpy.app.version_string,
            'python': p.python, 'bridge': str(BRIDGE), 'tools': bpy.path.abspath(p.tools),
            'reader': bpy.path.abspath(p.reader),
            'work': bpy.path.abspath(p.work), 'updated': time.time(),
            'games': {'GW1': p.gw1_path, 'GW2': p.gw2_path, 'BFN': p.bfn_path},
            'bfn_ready': bool(''.join(p.bfn_key.split()))}
    try:
        folder = link_dir()
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / 'link.json.tmp'
        tmp.write_text(json.dumps(data, indent=1), encoding='utf-8')
        tmp.replace(folder / 'link.json')
    except OSError as error:
        print('FrostyBlender link:', error)


class Handler(BaseHTTPRequestHandler):
    server_version = 'FrostyBlender'

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        # Browsers always send Origin on cross-site requests and cannot add this header without
        # a CORS preflight, which is never answered: only a local program gets through.
        if self.headers.get('Origin') is not None or self.headers.get('X-FrostyBlender') != '1':
            self._send(403, {'ok': False, 'error': 'Forbidden'})
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        if self.path == '/ping':
            self._send(200, {'ok': True, 'addon_version': _version(), 'blender_version': bpy.app.version_string,
                             'addon_update': True, 'restart_required': _restart_required})
        elif self.path == '/status':
            self._send(200, {'ok': True, 'tabs': _snapshot.get('tabs', {}), 'jobs': _jobs})
        elif self.path == '/armatures':
            self._send(200, {'ok': True, 'armatures': _snapshot.get('armatures', [])})
        else:
            self._send(404, {'ok': False, 'error': 'Not found'})

    def do_POST(self):
        if not self._allowed():
            return
        if self.path not in ('/import', '/addon-update'):
            self._send(404, {'ok': False, 'error': 'Not found'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4 << 20:
                raise ValueError('Bad request size')
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError('Expected a JSON object')
            if self.path == '/addon-update':
                request['kind'] = 'addon_update'
                if not all(isinstance(request.get(k), str) for k in ('archive', 'digest', 'version')):
                    raise ValueError('Missing update archive, checksum or version')
            elif request.get('kind') not in KINDS or request.get('game') not in ('GW1', 'GW2', 'BFN'):
                raise ValueError('Unknown kind or game')
        except (ValueError, TypeError) as error:
            self._send(400, {'ok': False, 'error': str(error)})
            return
        _counter[0] += 1
        job = str(_counter[0])
        _jobs[job] = {'state': 'queued', 'message': 'Waiting for Blender', 'kind': request['kind']}
        _requests.put((job, request))
        self._send(202, {'ok': True, 'job': job})


class Server(ThreadingHTTPServer):
    """Owns its port alone: on Windows a reusable address would let a second Blender bind the same
    port, and the app could talk to either."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# Read on the server thread, so filled in by the main thread on every tick.
_snapshot = {}


def _snap():
    from . import _job as map_job, animations_ui, cosmetics_ui, effects_ui, npcs_ui, props_ui
    scene = bpy.context.scene
    if scene is None:
        return
    tabs = {'maps': (scene.frostbite.status, bool(map_job)),
            'cosmetics': (scene.frostbite_cosmetics.status, bool(cosmetics_ui._job)),
            'npcs': (scene.frostbite_npcs.status, bool(npcs_ui._job)),
            'props': (scene.frostbite_props.status, bool(props_ui._job)),
            'effects': (scene.frostbite_effects.status, bool(effects_ui._job)),
            'animations': (scene.frostbite_animations.status, bool(animations_ui._job))}
    active = bpy.context.view_layer.objects.active if bpy.context.view_layer else None
    arms = [o for o in scene.objects if o.type == 'ARMATURE']
    arms.sort(key=lambda o: o is not active)
    _snapshot['tabs'] = {k: {'status': s, 'busy': b} for k, (s, b) in tabs.items()}
    _snapshot['armatures'] = [{'name': o.name, 'bones': len(o.data.bones), 'active': o is active} for o in arms]
    for job_id, job in list(_jobs.items()):  # a sent job is done when its tab is idle again
        if job['state'] == 'running' and job['kind'] in tabs:
            status, busy = tabs[job['kind']]
            job['message'] = status
            if not busy:
                failed = status.startswith(('Failed', 'Blender import failed'))
                pending = _unreal.pop(job_id, None)
                if pending and not failed:  # stays running (the app keeps polling) until it's sent
                    job['message'] = 'Imported; sending to Unreal...'
                    _send_unreal(job, pending)
                else:
                    job['state'] = 'failed' if failed else 'done'


def _send_unreal(job, pending):
    """After an app import marked for Unreal: export the objects it added and send them to the editor."""
    from . import unreal_export
    try:
        with bpy.context.temp_override(**_override()):
            objects = [o for o in unreal_export.scene_objects(bpy.context, False) if o.name not in pending['before']]
            if not objects:
                raise ValueError('the import added no visible meshes')
            unreal_job, message = unreal_export.send_to_unreal(bpy.context, objects, pending['name'])
    except (ConnectionError, OSError, RuntimeError, ValueError) as error:
        job.update(state='failed', message='Imported in Blender, but not sent to Unreal: %s' % error)
        return
    job.update(state='done', message=message, unreal_job=unreal_job)


def _has_unreal():
    """Export for Unreal ships in dev builds only."""
    return Path(__file__).with_name('unreal_export.py').is_file()


def _unreal_name(request):
    for key in ('map', 'character', 'group'):
        if isinstance(request.get(key), dict) and request[key].get('name'):
            return request[key]['name']
    return 'FrostyBlender'


def _override():
    windows = bpy.context.window_manager.windows if bpy.context.window_manager else []
    return {'window': windows[0]} if windows and not bpy.context.window else {}


def _run(request):
    """Fill the request's tab and press its Import button; returns an error text or ''."""
    global _restart_required
    if _restart_required:
        return 'Add-on updated. Save your work and restart Blender before importing or updating again.'
    if request['kind'] == 'addon_update':
        from . import addon_update
        _snap()
        if any(tab['busy'] for tab in _snapshot.get('tabs', {}).values()):
            return 'Wait for all Blender imports to finish before updating the add-on'
        _restart_required = addon_update.install(
            request['archive'], request['digest'], request['version'], _version(), bpy.app.version,
            link_dir() / 'updates', Path(__file__).resolve().parent)
        return ''
    from . import animations_ui, cosmetics_ui, effects_ui, npcs_ui, props_ui
    scene, kind, game = bpy.context.scene, request['kind'], request['game']
    options = request.get('options') or {}
    if options.get('texture_size') in ('512', '1024', '2048'):
        scene.frostbite.texture_size = options['texture_size']
    if 'game_shaders' in options:
        scene.frostbite.game_shaders = bool(options['game_shaders'])
    with bpy.context.temp_override(**_override()):
        if kind == 'maps':
            state = scene.frostbite
            state.source = game
            state.maps.clear()
            entry = state.maps.add()
            m = request['map']
            entry.name, entry.package, entry.mod = m['name'], m['package'], m.get('mod', '')
            state.index = 0
            return _press(bpy.ops.frostbite.import_map)
        if kind in ('cosmetics', 'npcs'):
            module = cosmetics_ui if kind == 'cosmetics' else npcs_ui
            state = scene.frostbite_cosmetics if kind == 'cosmetics' else scene.frostbite_npcs
            state.source = game
            character, items = request['character'], request['items']
            slots = {}
            for item in items:
                slots.setdefault(item['slot'], []).append(item)
            with _injected(module, bpy.context, {'characters': [dict(character, slots=slots)]}):
                entry = state.characters.add()
                entry.name, entry.id, entry.team = character['name'], character['id'], character.get('team', '')
                if hasattr(entry, 'group'):
                    entry.group = character.get('group', '')
                state.char_index = 0
                module._fill_items(state, bpy.context)
                for item in state.items:
                    item.selected = True
                state.rigged = bool(options.get('rig', True))
                state.pro_rig = bool(options.get('pro_rig', False))
                if kind == 'npcs':
                    state.spawn_at_cursor = bool(options.get('spawn_at_cursor', True))
                return _press(bpy.ops.frostbite.cosmetics_import if kind == 'cosmetics' else bpy.ops.frostbite.npcs_import)
        if kind == 'props':
            state = scene.frostbite_props
            state.source = game
            m = dict(request['map'], props=request['items'])
            with _injected(props_ui, bpy.context, {'maps': [m]}):
                entry = state.maps.add()
                entry.name, entry.package, entry.count = m['name'], m['package'], len(m['props'])
                state.map_index = 0
                props_ui._fill_props(state, bpy.context)
                for prop in state.props:
                    prop.selected = True
                return _press(bpy.ops.frostbite.props_import)
        if kind == 'effects':
            state = scene.frostbite_effects
            state.source = game
            group = dict(request['group'], effects=request['items'])
            with _injected(effects_ui, bpy.context, {'groups': [group]}):
                entry = state.groups.add()
                entry.name, entry.count = group['name'], len(group['effects'])
                state.group_index = 0
                effects_ui._fill_effects(state, bpy.context)
                for effect in state.effects:
                    effect.selected = True
                state.at_cursor = bool(options.get('spawn_at_cursor', True))
                return _press(bpy.ops.frostbite.effects_import)
        if kind == 'animations':
            state = scene.frostbite_animations
            state.source = game
            character, clips = request['character'], request['items']
            target = _armature(scene, options.get('armature'), character.get('name', ''))
            if target is None:
                return 'Import the character first, or pick its armature in the app'
            with _injected(animations_ui, bpy.context, {'characters': [dict(character, clips=clips)]}):
                entry = state.characters.add()
                entry.name, entry.rig, entry.count = character['name'], character['rig'], len(clips)
                state.char_index = 0
                animations_ui._fill_clips(state, bpy.context)
                state.hide_additive = False
                for clip in state.clips:
                    clip.selected = True
                state.target = target
                state.set_fps = bool(options.get('set_fps', True))
                return _press(bpy.ops.frostbite.animations_import)
    return 'Unknown kind'


KINDS = ('maps', 'cosmetics', 'npcs', 'props', 'effects', 'animations')


class _injected:
    """Give a tab the app's discover result for this game while its Import button runs, then
    put back what the tab's own scan had found."""
    def __init__(self, module, context, data):
        self.module, self.key, self.data = module, module._game(context), data

    def __enter__(self):
        self.old = self.module._data.get(self.key)
        self.module._data[self.key] = self.data

    def __exit__(self, *exc):
        if self.old is None:
            self.module._data.pop(self.key, None)
        else:
            self.module._data[self.key] = self.old


def _press(operator):
    if not operator.poll():
        return 'Blender is still busy with the previous import of this kind'
    result = operator()
    return '' if 'FINISHED' in result else 'Blender refused the import; see the add-on tab for why'


def _armature(scene, name, character):
    arms = [o for o in scene.objects if o.type == 'ARMATURE']
    if name:
        return next((o for o in arms if o.name == name), None)
    active = bpy.context.view_layer.objects.active
    if active in arms:
        return active
    key = character.lower().replace(' ', '')
    return next((o for o in arms if o.name.lower().replace(' ', '').split('.')[0] == key), arms[0] if len(arms) == 1 else None)


def tick():
    """Main-thread work: run queued requests and refresh the snapshot the server reads."""
    try:
        while True:
            job, request = _requests.get_nowait()
            _jobs[job].update(state='running', message='Starting...')
            options = request.get('options') or {}
            if options.get('unreal') and request['kind'] in UNREAL_KINDS and _has_unreal():
                _unreal[job] = {'before': {o.name for o in bpy.data.objects}, 'name': _unreal_name(request)}
            try:
                error = _run(request)
            except Exception as exc:  # report, never kill the timer
                error = str(exc)
            if error:
                _unreal.pop(job, None)
                _jobs[job].update(state='failed', message=error)
            elif request['kind'] == 'addon_update':
                _jobs[job].update(state='done', message='Add-on updated. Save your work and restart Blender.')
    except queue.Empty:
        pass
    try:
        _snap()
        if _server and time.time() - _last_link[0] > 5:  # keep paths current after preference edits
            _last_link[0] = time.time()
            write_link(_server.server_address[1])
    except Exception as exc:
        print('FrostyBlender link:', exc)
    return 0.5 if _server else None


def start(port=DEFAULT_PORT):
    global _server
    if _server:
        return True
    # Hyper-V, WSL and Docker reserve random port blocks (bind fails with access denied), so step
    # past reserved ports; stop at one in use, which is another Blender holding the link.
    for candidate in range(port, min(port + PORT_TRIES, 65536)):
        try:
            _server = Server(('127.0.0.1', candidate), Handler)
            break
        except OSError as error:
            in_use = getattr(error, 'winerror', None) == 10048 or error.errno == errno.EADDRINUSE
            print('FrostyBlender link: port %d unavailable (%s)%s' % (
                candidate, error, '; another Blender may hold it' if in_use else ''))
            if in_use:
                break
    if not _server:
        return False
    threading.Thread(target=_server.serve_forever, name='FrostyBlender link', daemon=True).start()
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=0.5, persistent=True)
    return True


def stop():
    global _server
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)


def running():
    return _server is not None
