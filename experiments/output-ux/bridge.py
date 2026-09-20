"""Small UX stimulus: familiar scripts, real remote execution, local artifacts.
No claim of general routing, concurrent-safe writeback, or interruption recovery.
"""
import difflib,hashlib,json,os,subprocess,sys,time,uuid
from pathlib import Path
root=Path.cwd();action=sys.argv[1]
assert action in ['build','generate','test']
run=uuid.uuid4().hex;directory=root/'.pandora-artifacts'/run;directory.mkdir(parents=True)
schema=json.loads((root/'src/schema.json').read_text());fixture_text=(root/'fixtures/receipt.json').read_text()
request={'id':run,'action':action,'schema':schema,'fixture':json.loads(fixture_text)}
encoded=json.dumps(request);start=time.monotonic()
print('[pandora] Running remotely; run '+run,flush=True)
r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','ubuntu@40.160.93.34','python3 /home/ubuntu/pandora-output-ux/remote.py'],input=encoded,capture_output=True,text=True,timeout=90)
(directory/'transport.stderr').write_text(r.stderr)
if r.returncode:
 print('[pandora] Transport failed: '+r.stderr,file=sys.stderr);sys.exit(70)
result=json.loads(r.stdout)
for path,contents in result['files'].items():
 assert path in {'dist/report.json','fixtures/receipt.json'}
 dest=directory/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(contents)
 hashes={path:hashlib.sha256(contents.encode()).hexdigest()}
(directory/'result.json').write_text(json.dumps(result,indent=2))
status=result['exit']
if action=='build' and not status:
 (root/'dist').mkdir(exist_ok=True)
 # This fixture starts with no output. Existing outputs are deliberately refused.
 try:
  with (root/'dist/report.json').open('x') as f:f.write(result['files']['dist/report.json'])
 except FileExistsError:
  print('[pandora] Existing build output was preserved; new output: '+str(directory/'dist/report.json'));status=75
 else:print('[pandora] Build succeeded. Local output: '+str(root/'dist/report.json'))
elif action=='generate' and not status:
 contents=result['files']['fixtures/receipt.json']
 patch=''.join(difflib.unified_diff(fixture_text.splitlines(True),contents.splitlines(True),fromfile='a/fixtures/receipt.json',tofile='b/fixtures/receipt.json'))
 (directory/'changes.patch').write_text(patch)
 print('[pandora] Remote generation succeeded. Workspace source was NOT changed.')
 print('[pandora] Action required: review and apply the returned changes, then run pnpm test.')
 print('[pandora] Diff: '+str(directory/'changes.patch'))
 print('[pandora] Generated file: '+str(directory/'fixtures/receipt.json'))
 status=75
else:print('[pandora] '+result['message'])
event={'id':run,'action':action,'request_sha256':hashlib.sha256(encoded.encode()).hexdigest(),'seconds':round(time.monotonic()-start,3),'remote_exit':result['exit'],'local_exit':status}
with (root/'.pandora-artifacts/events.jsonl').open('a') as f:f.write(json.dumps(event)+'\n')
sys.exit(status)
