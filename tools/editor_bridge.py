"""Background editor bridge. File paths are passed as argv, never shell commands."""
import argparse, json, os, re, subprocess, sys
from pathlib import Path
import gw1, gw2, bfn

def discover(game):
    if gw1.is_gw1(game):
        return gw1.discover(game)
    if (Path(game)/'GW2.Main_Win64_Retail.exe').is_file():
        return gw2.discover(game)
    if bfn.is_bfn(game):
        return bfn.discover(game)
    maps=[];root=Path(game)/'Mods'
    if not root.is_dir():return maps
    for mod in sorted(root.iterdir()):
        meta=mod/'reskate-levels.json'
        if not meta.is_file() or not (mod/'layout.toc').is_file():continue
        try:
            for level in json.loads(meta.read_text(encoding='utf-8-sig')).get('levels',[]):
                maps.append({'name':level.get('displayName',mod.name),'package':level['asset'],'mod':str(mod)})
        except (ValueError,KeyError,OSError):continue
    return maps

def execute(request,response):
    folder=response.parent
    folder.mkdir(parents=True,exist_ok=True)
    def progress(message):
        target=folder/'progress.json';tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps({'message':message}));tmp.replace(target)
    def run(args,stage,allowed=(0,),env=None):
        progress(stage)
        with (folder/(stage.split()[0].lower()+'.log')).open('w',encoding='utf-8') as log:
            code=subprocess.run([str(a) for a in args],env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).returncode
        if code not in allowed:raise RuntimeError(stage+' failed; see job logs in '+str(folder))
    if request['action']=='discover':return {'ok':True,'action':'discover','maps':discover(request['game_root'])}
    game=Path(request['game_root']);mod=Path(request['mod_root']);assets=Path(request['assets']);levels=Path(request['levels'])
    package=request['package'];mapname=package.rstrip('/').split('/')[-1]
    if not mapname or not re.fullmatch(r'[A-Za-z0-9_-]+',mapname):raise ValueError('Invalid map package name')
    exported=folder/'export';textures=folder/'textures';scene=folder/'scene.json'
    profile='gw2' if (game/'GW2.Main_Win64_Retail.exe').is_file() else 'skate'
    if profile=='gw2':
        run([sys.executable,Path(__file__).with_name('gw2.py'),'--game',game,'--map',package,'--out',exported],'Reading archives')
    else:
        run([request['reader'],game,mod,exported],'Reading archives')
    texture_env=dict(os.environ)
    if profile=='gw2':texture_env.setdefault('AURA_IMPORT_TEXTURE_SIZE','512')
    run([request['reader'],'--textures',exported,textures],'Decoding textures',(0,3),env=texture_env)
    converter='gw2_convert.py' if profile=='gw2' else 'convert.py'
    run([sys.executable,Path(__file__).with_name(converter),'--export',exported,'--textures',textures,'--assets',assets,'--out',scene,'--map',mapname],'Converting geometry')
    report=json.loads(scene.with_suffix('.report.json').read_text());leveldoc=json.loads(scene.read_text())
    if report['errors']:raise RuntimeError('Conversion contains '+str(len(report['errors']))+' errors; inspect '+str(scene.with_suffix('.report.json')))
    levels.mkdir(parents=True,exist_ok=True)
    name='frostbite_'+re.sub(r'[^A-Za-z0-9_-]+','_',leveldoc['name']);level=levels/(name+'.alvl');suffix=2
    while level.exists():level=levels/(name+'_'+str(suffix)+'.alvl');suffix+=1
    run([request['writer'],scene,level],'Writing ALVL')
    try:
        run([request['writer'],'--check',assets,level],'Validating assets')
    except Exception:
        level.unlink(missing_ok=True)
        raise
    progress('Import complete')
    return {'ok':True,'action':'import','level':str(level),'report':str(scene.with_suffix('.report.json')),
            'imported':report['placements'],'meshes':report['meshes'],'textures':report['textures']}

def main():
    p=argparse.ArgumentParser();p.add_argument('--request',type=Path,required=True);p.add_argument('--response',type=Path,required=True);a=p.parse_args()
    try:result=execute(json.loads(a.request.read_text(encoding='utf-8-sig')),a.response);code=0
    except Exception as e:result={'ok':False,'error':str(e)};code=1
    a.response.parent.mkdir(parents=True,exist_ok=True);a.response.write_text(json.dumps(result,indent=2));return code
if __name__=='__main__':raise SystemExit(main())
