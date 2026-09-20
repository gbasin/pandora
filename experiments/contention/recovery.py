"""Scripted client-loss and duplicate-submission probes during agent contention.

Only the owned non-agent transport child receives fault-injection signals.
Agent lane lifecycle remains with agent-fanout.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

p=argparse.ArgumentParser()
p.add_argument('--repo',type=Path,required=True)
p.add_argument('--state',type=Path,required=True)
p.add_argument('--profile',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--host',required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'warm'))
from transport import query
key=hashlib.sha256(str(a.repo.resolve()).encode()).hexdigest()
active=a.state/key/'active.json'
command=[sys.executable,'-B',str(root/'routing/launch.py'),'--host',a.host,'--state',str(a.state),'--docker-profile',str(a.profile),'--','docker']
results=[]


def invoke(args,label):
    with (a.output/(label+'.log')).open('w') as log:
        return subprocess.run(command+args,cwd=a.repo,stdout=log,stderr=subprocess.STDOUT,timeout=420).returncode


def save(record):
    results.append(record)
    (a.output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(record),flush=True)


assert invoke(['build','-t','app:test','.'],'build')==0
for mode,needle in [('queued-disconnect','queued; worker occupied'),('running-disconnect','CHECK_STARTED')]:
    old=json.loads(active.read_text())['attempt']
    args=['run','--rm','app:test']
    with (a.output/(mode+'.log')).open('w') as log:
        child=subprocess.Popen(command+args,cwd=a.repo,stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+360
        record=None
        while time.monotonic()<deadline:
            assert child.poll() is None,'Client exited before injection'
            if active.exists():
                candidate=json.loads(active.read_text())
                if candidate['attempt']!=old:
                    status=query(a.host,candidate['attempt'])
                    if needle in status.get('stdout','')+status.get('stderr',''):
                        record=candidate;break
                    if mode=='queued-disconnect' and 'CHECK_STARTED' in status.get('stdout',''):
                        raise AssertionError('No contention observed; queued-loss case not exercised')
            time.sleep(1)
        assert record,'Readiness deadline exceeded'
        attempt=record['attempt']
        assert invoke(args,mode+'-duplicate')==75
        assert json.loads(active.read_text())['attempt']==attempt
        # This driver owns child (route.py), whose sole child is the warm client.
        # These are scripted test processes, not coding-agent or fanout processes.
        owned=subprocess.check_output(['pgrep','-P',str(child.pid)],text=True).split()
        assert len(owned)==1,owned
        os.killpg(int(owned[0]),signal.SIGKILL)
        assert child.wait(timeout=30)==137
        assert invoke(['run','--rm','app:test','node','-e','process.exit(0)'],mode+'-changed')==75
        assert json.loads(active.read_text())['attempt']==attempt
        assert invoke(args,mode+'-retry')==0
        recovered=json.loads(active.read_text())
        assert recovered['attempt']==attempt and recovered['state']=='terminal'
        assert json.loads((a.repo/'dist/result.json').read_text())['value']=='recovery'
        terminal=query(a.host,attempt)
        assert terminal['cleanup_verified'] and terminal['exit_code']==0
        output=Path(record['output'])
        save({'case':mode,'attempt':attempt,'duplicate_exit':75,'changed_request_exit':75,
              'lost_client_exit':137,'retry_exit':0,'same_attempt':True,
              'metrics':json.loads((output/'metrics.json').read_text()),
              'terminal':json.loads((output/'terminal.json').read_text())})
