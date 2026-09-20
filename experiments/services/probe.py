"""Run one real Eichler journey with external services in a private namespace.
Evaluator probe only: no Docker socket in execution containers; no host ports.
"""
import argparse,hashlib,json,os,signal,subprocess,tarfile,tempfile,time,uuid
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--runner',required=True);p.add_argument('--fault',action='store_true');p.add_argument('--journey-file');p.add_argument('--cancel-after',type=int);a=p.parse_args()
source=Path(a.source);out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
name='pandora-service-'+uuid.uuid4().hex[:10]; containers=[];records=[]
def call(label,args,check=True,timeout=180):
 start=time.monotonic();r=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=timeout)
 (out/(label+'.stdout')).write_text(r.stdout);(out/(label+'.stderr')).write_text(r.stderr)
 records.append({'step':label,'seconds':round(time.monotonic()-start,3),'exit':r.returncode})
 print(json.dumps(records[-1]),flush=True)
 if check and r.returncode:raise RuntimeError(label+': '+r.stderr[-1400:])
 return r
def deadline(signum,frame): raise TimeoutError('Probe interrupted or deadline reached')
signal.signal(signal.SIGTERM,deadline);signal.signal(signal.SIGALRM,deadline);signal.alarm(900)
try:
 call('network',['network','create',name])
 image='pandora-deps:6deff854ef67e244ca612c50defe348881057c475c282c4acf10775e1921aec1'
 runner=name+'-run';containers.append(runner)
 call('runner',['run','-d','--name',runner,'--network',name,'--network-alias','pgbouncer','--cpus=2','--memory=6g','--memory-swap=6g','--pids-limit=512','--init','-e','CI=true','-e','WRANGLER_SEND_METRICS=false','-e','DATABASE_OWNER_URL=postgres://ike_owner:local-owner@127.0.0.1:5432/ike',image,'sleep','1200'])
 with tempfile.TemporaryDirectory() as tmp:
  tar=Path(tmp)/'source.tar'
  # Preserve dependency input timestamps installed in the matching image.
  with tarfile.open(tar,'w') as archive:
   for f in sorted(source.rglob('*')):
    rel=f.relative_to(source).as_posix()
    if f.is_file() and (f.name=='package.json' or rel in {'pnpm-lock.yaml','pnpm-workspace.yaml','.npmrc','.pnpmfile.cjs','pnpmfile.cjs'} or rel.startswith('patches/')):continue
    info=archive.gettarinfo(str(f),arcname=rel);info.uid=info.gid=1000;info.uname=info.gname='node'
    if info.isfile():
     with f.open('rb') as contents:archive.addfile(info,contents)
    else:archive.addfile(info)
  call('source-copy',['cp',str(tar),runner+':/tmp/source.tar'])
  call('source-extract',['exec',runner,'tar','xf','/tmp/source.tar','-C','/workspace/source'])
 call('script',['cp',a.runner,runner+':/workspace/source/pandora-service-probe.mjs'])
 if a.journey_file:
  call('journey-source-copy',['cp',a.journey_file,runner+':/workspace/source/packages/scenarios/src/journeys/S0-01.ts'])
 # The optional override was edited locally and transferred as a separate input.
 specs=[('db','postgres:16',['POSTGRES_USER=ike_owner','POSTGRES_PASSWORD=local-owner','POSTGRES_DB=ike','POSTGRES_HOST_AUTH_METHOD=password'],'768m'),('pool','edoburu/pgbouncer:latest',['DB_HOST=127.0.0.1','DB_PORT=5432','DB_USER=ike_application','DB_PASSWORD=local-application','AUTH_TYPE=plain','POOL_MODE=transaction','LISTEN_PORT=6432'],'256m'),('proxy','ghcr.io/neondatabase/wsproxy:latest',['LISTEN_PORT=:5433','ALLOW_ADDR_REGEX=^pgbouncer:6432$','APPEND_PORT=','LOG_TRAFFIC=false','LOG_CONN_INFO=false'],'128m')]
 for short,img,env,mem in specs:
  n=name+'-'+short;containers.append(n)
  call('start-'+short,['run','-d','--name',n,'--network','container:'+runner,'--cpus=.5','--memory='+mem,'--memory-swap='+mem,'--pids-limit=128',*[v for item in env for v in ['-e',item]],img],timeout=240)
  if short=='db':
   for attempt in range(60):
    r=subprocess.run(['docker','exec',n,'pg_isready','-U','ike_owner','-d','ike'],capture_output=True)
    if not r.returncode:break
    time.sleep(.5)
   else:raise RuntimeError('database readiness deadline')
 call('dependency-gate',['exec','-w','/workspace/source',runner,'node','tools/check-worktree-deps.mjs'])
 if a.cancel_after:signal.alarm(a.cancel_after)
 r=call('journey',['exec','-w','/workspace/source/packages/scenarios',runner,'node','--import','tsx','/workspace/source/pandora-service-probe.mjs'],check=False,timeout=600)
 (out/'result.json').write_text(json.dumps({'exit':r.returncode,'fault':a.fault,'source':str(source),'namespace':name,'journey_override_sha256':hashlib.sha256(Path(a.journey_file).read_bytes()).hexdigest() if a.journey_file else None,'records':records},indent=2))
 if a.fault and 'Pandora seeded journey fault' not in r.stdout+r.stderr:raise RuntimeError('Expected seeded failure evidence missing')
 if r.returncode!=(1 if a.fault else 0):raise RuntimeError('Unexpected journey result')
finally:
 signal.alarm(0)
 for n in reversed(containers):
  call('inspect-'+n,['inspect',n],check=False,timeout=20)
  call('remove-'+n,['rm','-f','-v',n],check=False,timeout=30)
 call('remove-network',['network','rm',name],check=False,timeout=30)
 (out/'steps.json').write_text(json.dumps(records,indent=2))
