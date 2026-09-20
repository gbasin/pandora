"""Real pnpm dependency and TypeScript-build cache probe, isolated from Eichler."""
import json,subprocess,tempfile,time,uuid,signal,argparse
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()
out=Path(a.output);out.mkdir(parents=True,exist_ok=False)
name='pandora-deps-probe-'+uuid.uuid4().hex[:10];tag=name+':test';records=[]
base='node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'
def call(label,args,timeout=180,check=True):
 start=time.monotonic();r=subprocess.run(['docker',*args],text=True,capture_output=True,timeout=timeout)
 for suffix,s in [('stdout',r.stdout),('stderr',r.stderr)]: (out/(label+'.'+suffix)).write_text(s)
 records.append(dict(step=label,seconds=round(time.monotonic()-start,3),exit=r.returncode));print(json.dumps(records[-1]),flush=True)
 if check and r.returncode:raise RuntimeError(label+': '+r.stderr[-1000:])
 return r
def stop(signum,frame):raise TimeoutError('Probe deadline or interruption')
signal.signal(signal.SIGALRM,stop);signal.signal(signal.SIGTERM,stop);signal.alarm(900)
try:
 call('builder',['buildx','create','--name',name,'--driver=docker-container','--driver-opt','memory=2g,memory-swap=2g,cpu-period=100000,cpu-quota=200000'])
 call('bootstrap',['buildx','inspect',name,'--bootstrap'])
 with tempfile.TemporaryDirectory(prefix=name) as directory:
  root=Path(directory)
  # Lockfile creation is separate from the timed frozen install builds.
  def manifest(version):
   (root/'package.json').write_text(json.dumps({'name':'pandora-dependency-probe','private':True,'dependencies':{'zod':version},'devDependencies':{'typescript':'5.6.3'}}))
   call('lock-'+version,['run','--rm','--name',name+'-lock','--cpus=1','--memory=512m','--memory-swap=512m','-v',str(root)+':/app','-w','/app',base,'sh','-c','npm install -g pnpm@12.3.4 --loglevel=error && pnpm install --lockfile-only --ignore-scripts --store-dir=/tmp/store'])
   (out/('lock-'+version+'.yaml')).write_bytes((root/'pnpm-lock.yaml').read_bytes())
  (root/'Dockerfile').write_text('FROM '+base+'''
RUN npm install -g pnpm@12.3.4 --loglevel=error
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store pnpm install --frozen-lockfile --ignore-scripts --store-dir=/pnpm/store --reporter=append-only
COPY source.ts ./
RUN pnpm exec tsc source.ts --outDir dist --target ES2022 --module commonjs --skipLibCheck
CMD ["node", "dist/source.js"]
''')
  (root/'.dockerignore').write_text('node_modules\ndist\n')
  def build(label):return call(label,['buildx','build','--builder',name,'--load','--provenance=false','--progress=plain','-t',tag,str(root)],timeout=300)
  def run(label,want):
   result=call(label,['run','--rm','--name',name+'-run','--network=none','--cpus=.5','--memory=128m','--memory-swap=128m',tag])
   assert result.stdout.strip()==want
  manifest('3.23.8')
  (root/'source.ts').write_text('import { z } from "zod"; console.log(z.string().parse("original"));\n')
  build('cold-build');run('run-original','original')
  build('identical-build');run('run-identical','original')
  (root/'source.ts').write_text('import { z } from "zod"; console.log(z.string().parse("edited"));\n')
  build('source-edit-build');run('run-edited','edited')
  manifest('3.24.2');build('dependency-edit-build');run('run-dependency-edit','edited')
 (out/'result.json').write_text(json.dumps({'records':records,'assertions':'passed','scope':'Small real TypeScript/pnpm fixture; not Eichler or long-build benchmark'},indent=2))
finally:
 signal.alarm(0)
 call('remove-run',['rm','-f',name+'-run',name+'-lock'],check=False)
 call('remove-image',['image','rm',tag],check=False)
 call('remove-builder',['buildx','rm',name],check=False)
 (out/'steps.json').write_text(json.dumps(records,indent=2))
