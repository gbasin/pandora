import json,os,subprocess,time
from pathlib import Path
poc=Path(__file__).resolve().parent;env=dict(os.environ,MUTAGEN_DATA_DIRECTORY=str(poc/'state'));name='pandora-allowlist-probe'
def sync(*args):return subprocess.run([str(poc/'mutagen'),'sync',*args],env=env,check=True,capture_output=True,text=True,timeout=60)
def remote(code):return subprocess.check_output(['ssh','-o','BatchMode=yes','ubuntu@40.160.93.34','python3 -'],input=code,text=True)
def pids():
 text=subprocess.check_output(['ssh','-o','BatchMode=yes','ubuntu@40.160.93.34','ps -eo pid,args'],text=True)
 return {int(line.split()[0]) for line in text.splitlines() if '.mutagen/agents/0.18.1/mutagen-agent synchronizer' in line}
sync('pause',name);before=pids();sync('resume',name)
for _ in range(20):
 owned=pids()-before
 if owned:break
 time.sleep(.5)
assert len(owned)==1,owned
pid=owned.pop()
remote('import os,signal\nos.kill('+str(pid)+',signal.SIGTERM)\n')
(poc/'fixture/sub/yes.txt').write_text('after-disconnect\n')
started=time.monotonic();failures=[]
for _ in range(5):
 try:sync('flush',name);break
 except subprocess.CalledProcessError as error:
  failures.append(error.stderr);time.sleep(2)
else:raise AssertionError(failures)
sync('pause',name)
value=remote("from pathlib import Path\nprint((Path.home()/'pandora-backend-poc/allowlist-probe/sub/yes.txt').read_text(),end='')\n")
assert value=='after-disconnect\n'
(poc/'disconnect-results.json').write_text(json.dumps({'remote_agent_killed':True,'flush_failures_before_recovery':failures,'flush_reconnected_seconds':time.monotonic()-started,'value':value},indent=2)+'\n')
