"""Evaluator-only remote fixture execution; not production submission/recovery."""
import fcntl,json,subprocess,sys
from pathlib import Path
request=json.load(sys.stdin)
assert request['action'] in ['build','generate','test']
assert len(request['id'])==32 and all(c in '0123456789abcdef' for c in request['id'])
name='pandora-output-ux-'+request['id']
program='''let input=""; process.stdin.on("data",x=>input+=x);process.stdin.on("end",()=>{
const r=JSON.parse(input), s=r.schema;
const value={title:s.title,currency:s.currency,totalCents:s.subtotalCents+s.taxCents};
if(r.action==="generate") value.note=r.fixture.note;
if(r.action==="test") {
 const wanted={...value,note:r.fixture.note};
 const ok=JSON.stringify(wanted)===JSON.stringify(r.fixture);
 console.log(JSON.stringify({exit:ok?0:1,message:ok?"Receipt fixture matches schema":"Receipt fixture differs from schema",files:{}}));
} else console.log(JSON.stringify({exit:0,message:r.action+" completed",files:{[r.action==="build"?"dist/report.json":"fixtures/receipt.json"]:JSON.stringify(value,null,2)+"\\n"}}));
});'''
with (Path.home()/'pandora-warm/worker.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 try:
  result=subprocess.run(['sudo','docker','run','--rm','-i','--name',name,'--network=none','--cpus=.5','--memory=128m','--memory-swap=128m','--pids-limit=64','node:24-bookworm-slim','node','-e',program],input=json.dumps(request),capture_output=True,text=True,timeout=60)
  if result.returncode:raise RuntimeError(result.stderr)
  print(result.stdout,end='')
 finally:subprocess.run(['sudo','docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
