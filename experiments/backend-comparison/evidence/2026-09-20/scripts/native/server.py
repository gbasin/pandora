import fcntl,json,subprocess,sys
from pathlib import Path
root=Path.home()/'pandora-warm'
lock=(root/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX)
poc=Path.home()/'pandora-backend-poc';poc.mkdir(exist_ok=True)
socket=poc/'sockets';socket.mkdir(exist_ok=True)
def docker(*args,**kwargs):return subprocess.run(['sudo','docker',*args],check=True,**kwargs)
names=['pandora-poc-native-builder','pandora-poc-registry']
try:
 for name in names:
  if subprocess.run(['sudo','docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
   docker('start',name,stdout=subprocess.DEVNULL)
  elif name.endswith('registry'):
   docker('run','-d','--name',name,'--network=host','--memory=256m','--memory-swap=256m','--cpus=1',
          '-e','REGISTRY_HTTP_ADDR=127.0.0.1:15000','-v','pandora-poc-registry:/var/lib/registry','registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373',stdout=subprocess.DEVNULL)
  else:
   docker('run','-d','--name',name,'--privileged','--network=host','--memory=6g','--memory-swap=6g','--cpus=2',
          '-v','buildx_buildkit_pandora-docker-builds-v10_state:/var/lib/buildkit',
          '-v',str(socket)+':/run/pandora-poc',
          'moby/buildkit@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8',
          '--addr','unix:///run/pandora-poc/buildkit.sock','--group','1000',stdout=subprocess.DEVNULL)
 print(json.dumps({'ready':True,'socket':str(socket/'buildkit.sock')}),flush=True)
 for line in sys.stdin:
  request=json.loads(line)
  if request['action']=='import':
   tag=request['tag'];docker('pull',tag,stdout=sys.stderr)
   identity=docker('image','inspect',tag,'--format','{{.Id}}',capture_output=True,text=True).stdout.strip()
   print(json.dumps({'image':identity}),flush=True)
  elif request['action']=='finish':break
finally:
 for name in names:subprocess.run(['sudo','docker','stop','-t','5',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
