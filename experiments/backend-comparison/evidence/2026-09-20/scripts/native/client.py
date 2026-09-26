import json,os,re,subprocess,sys,time,uuid
from pathlib import Path
base=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(base/'experiments/warm'))
from snapshot import freeze
poc=Path(__file__).resolve().parent
repo=Path('/Users/you/Code/acme/.worktrees/pandora-compiled-build')
run=poc/uuid.uuid4().hex;run.mkdir()
start=time.monotonic()
manifest,_=freeze(repo,run/'source')
expected='source-edit-20260920'
source_edit=os.environ.get('POC_SOURCE_EDIT')=='1'
if source_edit:
 changed=run/'source/apps/web/index.html';before_stat=changed.stat()
 expected='source-edit-20260921';changed.write_text(changed.read_text().replace('source-edit-20260920',expected))
 os.utime(changed,ns=(before_stat.st_atime_ns,before_stat.st_mtime_ns))
context=run/'source'
stable=os.environ.get('POC_STABLE')=='1'
if stable:
 context=poc/'stable-source';context.mkdir(exist_ok=True)
 subprocess.run(['rsync','-rlpc','--delete',str(run/'source')+'/',str(context)+'/'],check=True)
frozen=time.monotonic()
fault=os.environ.get('POC_DISCONNECT')=='1'
if fault:
 context=run/'fault-context';context.mkdir()
 recipe=context/'Pandora.Dockerfile'
 recipe.write_text('FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6\nRUN echo PANDORA_FAULT_'+run.name+' && sleep 30\n')

env=dict(os.environ,DOCKER_CONFIG=str(poc/'docker-config'),BUILDX_CONFIG=str(poc/'buildx-config'))
Path(env['DOCKER_CONFIG']).mkdir(exist_ok=True)
log=(run/'transport.log').open('w')
server=subprocess.Popen(['ssh','-o','BatchMode=yes','ubuntu@WORKER','python3 -u pandora-native-poc.py'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True)
tunnel=None
buildx='/Applications/Docker.app/Contents/Resources/cli-plugins/docker-buildx'
builder='pandora-poc-native-stable' if stable else 'poc-'+run.name
sock='/tmp/pandora-native-stable.sock' if stable else '/tmp/pandora-native-'+run.name[:12]+'.sock'
try:
 ready=json.loads(server.stdout.readline());assert ready['ready']
 tunnel=subprocess.Popen(['ssh','-o','BatchMode=yes','-o','ExitOnForwardFailure=yes','-N','-L',sock+':'+ready['socket'],'ubuntu@WORKER'],stderr=log)
 for _ in range(100):
  if Path(sock).exists():break
  time.sleep(.1)
 if subprocess.run([buildx,'inspect',builder],env=env,stdout=log,stderr=log).returncode:
  subprocess.run([buildx,'create','--name',builder,'--driver','remote','unix://'+sock],env=env,stdout=log,stderr=log,check=True)
 tag='127.0.0.1:15000/pandora-poc:compiled'
 before=time.monotonic()
 with (run/'build.log').open('w') as output:
  child=subprocess.Popen([buildx,'build','--builder',builder,'--platform','linux/amd64','--provenance=false','--progress=plain','--metadata-file',str(run/'metadata.json'),'-f',str(context/'Pandora.Dockerfile'),'--output','type=image,name='+tag+',push=true,registry.insecure=true',str(context)],env=env,stdout=output,stderr=subprocess.STDOUT)
  if fault:
   for _ in range(180):
    if re.search(r'#\d+ \d+\.\d+ PANDORA_FAULT_'+run.name,(run/'build.log').read_text()):break
    if child.poll() is not None:raise AssertionError('Build ended before fault')
    time.sleep(1)
   else:raise TimeoutError('No fault readiness')
   tunnel.terminate();tunnel.wait(timeout=10)
  status=child.wait(timeout=300)
 built=time.monotonic()
 if fault:
  assert status!=0
  time.sleep(2)
  processes=subprocess.check_output(['ssh','-o','BatchMode=yes','ubuntu@WORKER','sudo docker top pandora-poc-native-builder -eo pid,args'],text=True)
  (run/'fault.json').write_text(json.dumps({'status':status,'seconds':time.monotonic()-start,'processes_after_disconnect':processes},indent=2)+'\n')
  print(run,flush=True)
  raise SystemExit(0)
 assert status==0, (run,status)
 server.stdin.write(json.dumps({'action':'import','tag':tag})+'\n');server.stdin.flush()
 image=json.loads(server.stdout.readline())['image'];imported=time.monotonic()
 # Equivalent subsequent foreground run, with the same resource limits.
 tested=subprocess.run(['ssh','-o','BatchMode=yes','ubuntu@WORKER','sudo docker run --rm --cpus=2 --memory=6g --memory-swap=6g --network=none '+image+' node check.cjs '+expected],capture_output=True,text=True,check=True)
 (run/'summary.json').write_text(json.dumps({'stable_context':stable,'source_edit_same_size_mtime':source_edit,'snapshot_seconds':frozen-start,'setup_seconds':before-frozen,'build_seconds':built-before,'import_seconds':imported-built,'ready_seconds':imported-start,'image':image,'test':tested.stdout},indent=2)+'\n')
 print(run,flush=True)
finally:
 if server.poll() is None:
  server.stdin.write('{"action":"finish"}\n');server.stdin.flush();server.stdin.close()
  server.wait(timeout=30)
 if tunnel is not None:
  tunnel.terminate();tunnel.wait(timeout=10)
 if not stable:subprocess.run([buildx,'rm',builder],env=env,stdout=log,stderr=log)
 Path(sock).unlink(missing_ok=True)
 log.close()
