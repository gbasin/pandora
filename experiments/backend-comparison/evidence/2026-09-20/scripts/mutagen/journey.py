import hashlib,json,os,subprocess,sys,time,uuid,shlex
from pathlib import Path
poc=Path(__file__).resolve().parent;base=poc.parents[1]
sys.path.insert(0,str(base/'experiments/warm'))
from snapshot import freeze,encode
from worker_bundle import bundle
from transport import SSH_OPTIONS,follow,query
repo=Path('/Users/garybasin/Code/eichler/.worktrees/pandora-journey-preflight')
env=dict(os.environ,MUTAGEN_DATA_DIRECTORY=str(poc/'state'))
mutagen=str(poc/'mutagen');host='ubuntu@40.160.93.34';ssh=['ssh',*SSH_OPTIONS,host]
def cmd(args,**kwargs):return subprocess.run(args,check=True,**kwargs)
def sync(*args):
 return cmd([mutagen,'sync',*args],env=env,capture_output=True,text=True,timeout=180)
start=time.monotonic();aid=uuid.uuid4().hex;out=poc/aid;out.mkdir()
manifest_only=os.environ.get('POC_MANIFEST_ONLY')=='1'
if manifest_only:
 from snapshot import names,excluded as is_excluded,entry
 all_names=names(repo);excluded=[name for name in all_names if is_excluded(name)]
 manifest=[e for name in all_names if not is_excluded(name) and (e:=entry(repo,name)) is not None]
else:
 manifest,excluded=freeze(repo,out/'source')
captured=time.monotonic()
# Static allowlist prevents new ignored/unlisted files from ever entering the mirror.
allowed=set()
for item in manifest:
 path=Path(item['path']);allowed.add(str(path));allowed.update(str(p) for p in path.parents if str(p)!='.')
def escape(name):
 for c in ['\\','[',']','*','?']:
  name=name.replace(c,'\\'+c)
 return name
patterns=['*']+['!/'+escape(name) for name in sorted(allowed,key=lambda n:(n.count('/'),n))]
config=json.dumps({'sync':{'defaults':{'ignore':{'paths':patterns}}}})
identity=hashlib.sha256(config.encode()).hexdigest()[:12];session='pandora-journey-'+identity
configpath=poc/(identity+'.json');configpath.write_text(config)
mirror='/home/ubuntu/pandora-backend-poc/journey-mirror'
if not (poc/(identity+'.session')).exists():
 sync('create','--name',session,'--no-global-configuration','--configuration-file',str(configpath),'--mode','one-way-replica','--ignore-vcs','--symlink-mode','portable',str(repo),host+':'+mirror)
 (poc/(identity+'.session')).write_text(session)
else:sync('resume',session)
sync('flush',session);sync('pause',session);synced=time.monotonic()
# Stage immutable manifest-selected bytes on the worker, then verify them.
bundleid,payload=bundle(base/'experiments/warm')
request={'identity':bundleid,'attempt_id':aid,'repo_key':None,'payload':payload}
script=(base/'experiments/warm/worker_bundle.py').read_text().split("if __name__ == '__main__':")[0]
script+='\nprint(json.dumps(prepare(Path.home()/"pandora-warm", **'+repr(request)+')))\n'
cmd([*ssh,'python3 -'],input=script,text=True,capture_output=True)
remote='/home/ubuntu/pandora-warm/runs/'+aid
meta={'profile':'integrated-surface-v1','attempt':aid,'source_digest':hashlib.sha256(encode(manifest)).hexdigest(),'excluded':excluded,'workflow':'journey','selectors':['S0-01'],'require_warm':False}
(out/'manifest.json').write_bytes(encode(manifest));(out/'submission.json').write_text(json.dumps(meta))
cmd(['scp',*SSH_OPTIONS,'-q',str(out/'manifest.json'),str(out/'submission.json'),host+':'+remote+'/'])
copy_script='''import json,shutil,sys
from pathlib import Path
attempt=Path(%r);mirror=Path(%r)
sys.path.insert(0,str(attempt));from snapshot import verify
manifest=json.loads((attempt/'manifest.json').read_text())
for item in manifest:
 src=mirror/item['path'];dst=attempt/'source'/item['path'];dst.parent.mkdir(parents=True,exist_ok=True)
 if src.is_symlink():dst.symlink_to(src.readlink())
 else:shutil.copy2(src,dst)
verify(attempt/'source',manifest)
'''%(remote,mirror)
try:
 cmd([*ssh,'python3 -'],input=copy_script,text=True,capture_output=True)
 # Preserve Pandora's before/after local mutation check without trusting sync status.
 from snapshot import names,excluded as is_excluded,entry
 current=[e for name in names(repo) if not is_excluded(name) and (e:=entry(repo,name)) is not None]
 assert current==manifest,'Source changed during synchronization; no execution launched'
finally:sync('resume',session)
ready=time.monotonic()
launch=(f'sudo systemd-run --quiet --collect --unit=pandora-worker-{aid} --uid=ubuntu --working-directory={remote} '
 '--property=RuntimeMaxSec=40m --property=TimeoutStopSec=30s --property=KillMode=control-group '
 f'--property=ExecStopPost={shlex.quote("/usr/bin/python3 "+remote+"/service_cleanup.py "+remote)} /bin/bash -c '+shlex.quote('exec python3 -u worker.py >stdout.log 2>stderr.log'))
cmd([*ssh,launch]);status=follow(host,out);assert status==0,status
assert query(host,aid,'release')['cleanup_verified']
(out/'timings.json').write_text(json.dumps({'attempt':aid,'manifest_only':manifest_only,'capture_seconds':captured-start,'sync_seconds':synced-captured,'prepare_verify_seconds':ready-synced,'ready_seconds':ready-start,'total_seconds':time.monotonic()-start,'session':session},indent=2)+'\n')
print(out,flush=True)
