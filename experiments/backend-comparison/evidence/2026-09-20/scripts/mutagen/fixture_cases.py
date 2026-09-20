import json,os,subprocess,time
from pathlib import Path
poc=Path(__file__).resolve().parent
binary=str(poc/'mutagen');env=dict(os.environ,MUTAGEN_DATA_DIRECTORY=str(poc/'state'))
fixture=poc/'fixture';host='ubuntu@40.160.93.34';name='pandora-allowlist-probe'
def sync(*args):return subprocess.run([binary,'sync',*args],env=env,check=True,capture_output=True,text=True,timeout=30)
def remote(code):return subprocess.check_output(['ssh','-o','BatchMode=yes',host,'python3 -'],input=code,text=True)
root='/home/ubuntu/pandora-backend-poc'
results={}
sync('resume',name);sync('flush',name);sync('pause',name)
# Independent immutable copy; no shared inodes with live mirror.
remote("from pathlib import Path\nimport shutil\nr=Path("+repr(root)+")\nshutil.copytree(r/'allowlist-probe',r/'frozen-fixture')\n")
(fixture/'sub/yes.txt').write_text('changed\n');(fixture/'sub/new.txt').write_text('new source\n')
(fixture/'nested').mkdir(exist_ok=True);(fixture/'nested/private.txt').write_text('nested worktree excluded\n')
sync('resume',name);sync('flush',name);sync('pause',name)
report=json.loads(remote("from pathlib import Path\nimport json\nr=Path("+repr(root)+")\nprint(json.dumps({'frozen':(r/'frozen-fixture/sub/yes.txt').read_text(),'live':(r/'allowlist-probe/sub/yes.txt').read_text(),'files':[str(p.relative_to(r/'allowlist-probe')) for p in (r/'allowlist-probe').rglob('*') if p.is_file()]}))\n"))
assert report['frozen']=='allowed\n' and report['live']=='changed\n'
assert report['files']==['sub/yes.txt'],report
results['isolation_and_exclusions']=report
# New allowed source membership requires session recreation; flush alone cannot add it.
sync('terminate',name)
config=poc/'allow-expanded.json';config.write_text(json.dumps({'sync':{'defaults':{'ignore':{'paths':['*','!/sub','!/sub/yes.txt','!/sub/new.txt']}}}}))
sync('create','--name',name,'--no-global-configuration','--configuration-file',str(config),'--mode','one-way-replica','--ignore-vcs',str(fixture),host+':'+root+'/allowlist-probe')
sync('flush',name);sync('pause',name)
files=json.loads(remote("from pathlib import Path\nimport json\nr=Path("+repr(root+'/allowlist-probe')+")\nprint(json.dumps(sorted(str(p.relative_to(r)) for p in r.rglob('*') if p.is_file())))\n"))
assert files==['sub/new.txt','sub/yes.txt'];results['new_file_after_recreation']=files
(poc/'fixture-results.json').write_text(json.dumps(results,indent=2)+'\n')
