import json,os,subprocess
from pathlib import Path
poc=Path(__file__).resolve().parent;env=dict(os.environ,MUTAGEN_DATA_DIRECTORY=str(poc/'state'))
def run(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,**kw)
def sync(*args):return run([str(poc/'mutagen'),'sync',*args],env=env,timeout=45)
repo=poc/'git-repo';repo.mkdir();run(['git','init','-q',str(repo)])
(repo/'value.txt').write_text('a\n');(repo/'.gitignore').write_text('nested/\n')
run(['git','-C',str(repo),'add','.']);run(['git','-C',str(repo),'-c','user.name=POC','-c','user.email=poc@example.com','commit','-qm','fixture'])
other=repo/'nested/worker';run(['git','-C',str(repo),'worktree','add','-qb','other',str(other)])
(other/'value.txt').write_text('b\n')
config=poc/'worktree-allow.json';config.write_text(json.dumps({'sync':{'defaults':{'ignore':{'paths':['*','!/value.txt','!/.gitignore']}}}}))
results=[]
for label,local in [('a',repo),('b',other)]:
 name='pandora-worktree-'+label;remote='/home/ubuntu/pandora-backend-poc/worktree-'+label
 sync('create','--name',name,'--no-global-configuration','--configuration-file',str(config),'--mode','one-way-replica','--ignore-vcs',str(local),'ubuntu@WORKER:'+remote)
 sync('flush',name);sync('pause',name)
 script='from pathlib import Path\nimport json\nr=Path('+repr(remote)+')\nprint(json.dumps({"value":(r/"value.txt").read_text(),"files":sorted(str(p.relative_to(r)) for p in r.rglob("*") if p.is_file())}))\n'
 result=json.loads(run(['ssh','-o','BatchMode=yes','ubuntu@WORKER','python3 -'],input=script).stdout)
 assert result['value']==label+'\n' and result['files']==['.gitignore','value.txt'];results.append(result)
(poc/'worktree-results.json').write_text(json.dumps(results,indent=2)+'\n')
