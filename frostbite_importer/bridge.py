"""Runs outside Blender with the system Python and drives aura-plugin-frostbite's tools.

    python bridge.py discover --tools TOOLS --game GAME --out maps.json
    python bridge.py import --tools TOOLS --reader READER --game GAME --mod MOD
                            --package PACKAGE --job JOB [--texture-size N] [--keep] [--bfn-types NAMES]
    python bridge.py cosmetics-discover --tools TOOLS --game GAME --out cosmetics.json
    python bridge.py props-discover --tools TOOLS --game GAME --out props.json  (props-import as cosmetics-import)
    python bridge.py npcs-discover --tools TOOLS --game GAME --out npcs.json  (npcs-import as cosmetics-import)
    python bridge.py animations-discover --tools TOOLS --game GAME --out clips.json
    python bridge.py animations-import --tools TOOLS --game GAME --items ITEMS.json --job JOB
    python bridge.py cosmetics-import --tools TOOLS --reader READER --game GAME --items ITEMS.json
                            --job JOB [--texture-size N] [--keep] [--bfn-types NAMES]

Battle for Neighborville also needs AURA_BFN_KEY (the initfs key, hex) in the environment.

Writes JOB/progress.json while running and JOB/result.json at the end.
The converted map lands in JOB/assets and JOB/scene.json.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import time
import sys
from pathlib import Path


def write_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:  # Windows refuses while Blender is reading the old file
            if attempt == 19:
                raise
            time.sleep(0.05)


def discover(args):
    sys.path.insert(0, str(args.tools))
    from editor_bridge import discover as find
    write_json(args.out, {'ok': True, 'maps': find(args.game)})


def steps(job):
    """progress(message, step) and run(command, stage, step, allowed, env) writing into job."""
    job.mkdir(parents=True, exist_ok=True)

    def progress(message, step):
        try:
            write_json(job / 'progress.json', {'message': message, 'step': step, 'steps': 3})
        except OSError:
            pass  # progress is only shown in the panel; never fail an import over it

    def run(command, stage, step, allowed=(0,), env=None):
        progress(stage, step)
        log_path = job / (stage.split()[0].lower() + '.log')
        with log_path.open('w', encoding='utf-8') as log:
            code = subprocess.run([str(c) for c in command], env=env, stdout=log, stderr=subprocess.STDOUT,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode
        if code not in allowed:
            tail = log_path.read_text(encoding='utf-8', errors='replace').strip().splitlines()[-1:]
            raise RuntimeError('%s failed: %s (log: %s)' % (stage, tail[0] if tail else 'exit %d' % code, log_path))
        return code
    return progress, run


def cosmetics_discover(args):
    sys.path.insert(0, str(args.tools))
    import cosmetics
    write_json(args.out, dict(cosmetics.discover(args.game), ok=True))


def props_discover(args):
    sys.path.insert(0, str(args.tools))
    import props
    write_json(args.out, dict(props.discover(args.game), ok=True))


def npcs_discover(args):
    sys.path.insert(0, str(args.tools))
    import npcs
    write_json(args.out, dict(npcs.discover(args.game), ok=True))


def animations_discover(args):
    sys.path.insert(0, str(args.tools))
    import animations
    write_json(args.out, dict(animations.discover(args.game), ok=True))


def animations_import(args):
    """Chosen clips -> one decoded clip JSON each, for the add-on to put on a rigged character."""
    job = args.job
    progress, run = steps(job)
    clips = job / 'clips'
    run([sys.executable, args.tools / 'animations.py', 'export', '--game', args.game, '--items', args.items,
         '--out', clips], 'Decoding clips', 1)
    report = json.loads((clips / 'clips.report.json').read_text(encoding='utf-8'))
    progress('Import complete', 3)
    return {'ok': True, 'clips': report['clips'], 'errors': len(report['errors']),
            'error_details': report['errors'][:5]}


def props_import(args):
    """Chosen props -> textures -> meshes laid out in a row."""
    return cosmetics_import(args, 'props.py')


def npcs_import(args):
    """Chosen NPC bundles -> textures -> meshes rigged to the NPC's skeleton."""
    return cosmetics_import(args, 'npcs.py')


def cosmetics_import(args, tool='cosmetics.py'):
    """Chosen cosmetic bundles -> textures -> meshes placed where the character's skeleton expects them."""
    job = args.job
    progress, run = steps(job)
    tools, game = args.tools, args.game
    exported, textures, assets, scene = job / 'export', job / 'textures', job / 'assets', job / 'scene.json'
    command = [sys.executable, tools / tool, 'export', '--game', game, '--items', args.items, '--out', exported]
    if (game / 'PVZBattleforNeighborville.exe').is_file():
        if not os.environ.get('AURA_BFN_KEY'):
            raise ValueError('Battle for Neighborville needs its initfs key (add-on preferences)')
        if not args.bfn_types or not args.bfn_types.is_file():
            raise ValueError('Battle for Neighborville needs its type name table (add-on preferences)')
        command += ['--names', args.bfn_types]
    run(command, 'Reading archives', 1)
    env = dict(os.environ, AURA_IMPORT_TEXTURE_SIZE=str(args.texture_size))
    run([args.reader, '--textures', exported, textures], 'Decoding textures', 2, (0, 3), env)
    run([sys.executable, tools / tool, 'convert', '--export', exported, '--textures', textures,
         '--assets', assets, '--out', scene], 'Converting meshes', 3)
    report = json.loads(scene.with_suffix('.report.json').read_text(encoding='utf-8'))
    if not args.keep:
        shutil.rmtree(exported, ignore_errors=True)
        shutil.rmtree(textures, ignore_errors=True)
    progress('Import complete', 3)
    return {'ok': True, 'scene': str(scene), 'assets': str(assets), 'errors': len(report.get('errors', [])),
            'meshes': report.get('meshes')}


def run_import(args):
    job = args.job
    progress, run = steps(job)

    tools, game = args.tools, args.game
    mapname = args.package.rstrip('/').split('/')[-1]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', mapname):
        raise ValueError('Invalid map package name')
    exported, textures, assets, scene = job / 'export', job / 'textures', job / 'assets', job / 'scene.json'
    gw1 = (game / 'PVZ.Main_Win64_Retail.exe').is_file()
    # GW1 shares GW2's archive family and converter; gw1.py adapts its texture headers.
    gw2 = gw1 or (game / 'GW2.Main_Win64_Retail.exe').is_file()
    bfn = (game / 'PVZBattleforNeighborville.exe').is_file()
    if bfn:
        # The initfs key travels in the environment, not on the command line.
        if not os.environ.get('AURA_BFN_KEY'):
            raise ValueError('Battle for Neighborville needs its initfs key (add-on preferences)')
        if not args.bfn_types or not args.bfn_types.is_file():
            raise ValueError('Battle for Neighborville needs its type name table (add-on preferences)')
        run([sys.executable, tools / 'bfn.py', '--game', game, '--map', args.package, '--out', exported,
             '--names', args.bfn_types], 'Reading archives', 1)
    elif gw2:
        run([sys.executable, tools / ('gw1.py' if gw1 else 'gw2.py'), '--game', game, '--map', args.package,
             '--out', exported], 'Reading archives', 1)
    else:
        run([args.reader, game, args.mod, exported], 'Reading archives', 1)
    env = dict(os.environ, AURA_IMPORT_TEXTURE_SIZE=str(args.texture_size))
    run([args.reader, '--textures', exported, textures], 'Decoding textures', 2, (0, 3), env)
    if bfn:
        run([sys.executable, tools / 'convert_legacy.py', '--export', exported, '--textures', textures,
             '--assets', assets, '--out', scene, '--level', args.package], 'Converting geometry', 3)
    else:
        converter = 'gw2_convert.py' if gw2 else 'convert.py'
        run([sys.executable, tools / converter, '--export', exported, '--textures', textures,
             '--assets', assets, '--out', scene, '--map', mapname], 'Converting geometry', 3, (0, 1))
    report = json.loads(scene.with_suffix('.report.json').read_text(encoding='utf-8'))
    if not args.keep:
        shutil.rmtree(exported, ignore_errors=True)
        shutil.rmtree(textures, ignore_errors=True)
    progress('Import complete', 3)
    return {'ok': True, 'scene': str(scene), 'assets': str(assets), 'errors': len(report.get('errors', [])),
            'placements': report.get('placements'), 'meshes': report.get('meshes'), 'textures': report.get('textures')}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('discover', 'import', 'cosmetics-discover', 'cosmetics-import',
                                      'props-discover', 'props-import', 'animations-discover', 'animations-import',
                                      'npcs-discover', 'npcs-import'))
    p.add_argument('--tools', type=Path, required=True)
    p.add_argument('--game', type=Path, required=True)
    p.add_argument('--out', type=Path)
    p.add_argument('--reader', type=Path)
    p.add_argument('--mod', type=Path)
    p.add_argument('--package')
    p.add_argument('--job', type=Path)
    p.add_argument('--texture-size', type=int, default=1024)
    p.add_argument('--keep', action='store_true')
    p.add_argument('--bfn-types', type=Path, help='Battle for Neighborville type name table (frosty_types.ps1)')
    p.add_argument('--items', type=Path, help='cosmetics-import: JSON list of items from cosmetics-discover')
    args = p.parse_args()
    if args.action.endswith('discover'):
        try:
            {'discover': discover, 'cosmetics-discover': cosmetics_discover, 'props-discover': props_discover,
             'animations-discover': animations_discover, 'npcs-discover': npcs_discover}[args.action](args)
        except Exception as error:
            write_json(args.out, {'ok': False, 'error': str(error)})
            return 1
        return 0
    try:
        result = {'import': run_import, 'cosmetics-import': cosmetics_import, 'props-import': props_import,
                  'animations-import': animations_import, 'npcs-import': npcs_import}[args.action](args)
        code = 0
    except Exception as error:
        result, code = {'ok': False, 'error': str(error)}, 1
    write_json(args.job / 'result.json', result)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
