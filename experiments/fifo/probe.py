"""Twelve real worker admissions, with cancellation, deadline and process-loss faults."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'warm'))
from transport import query

p=argparse.ArgumentParser()
p.add_argument('--host',required=True)
p.add_argument('--repo',type=Path,required=True)
p.add_argument('--spec',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
base=json.loads(a.spec.read_text())
children=[]
handles=[]
ids=[]
recovered=None
success=False


def remote(command):
    return subprocess.check_output(['ssh','-o','BatchMode=yes',a.host,command],text=True,timeout=20)


def until(predicate,seconds=90):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=predicate()
        if result:return result
        time.sleep(.5)
    raise TimeoutError('Readiness deadline')


def state(identity):
    return query(a.host,identity)


def registered(identity):
    result=state(identity)
    return result if 'admitted to FIFO queue' in result.get('stdout','') else None


try:
    for i in range(12):
        identity=uuid.uuid4().hex;ids.append(identity)
        spec=json.loads(json.dumps(base))
        spec.pop('image',None)
        code=f"console.log('FIFO_START {i} '+Date.now());setTimeout(()=>console.log('FIFO_END {i}'),{60000 if i==0 else 2000})"
        spec['request']={'kind':'run','tag':'compiled:test','mount':None,'command':['node','-e',code]}
        # No outputs are produced by this scheduling probe.
        spec['config']['outputs']=[]
        spec['config']['queue_timeout_seconds']=2 if i==4 else 120
        log=(a.output/f'{i}.log').open('w');handles.append(log)
        command=[sys.executable,'-B',str(ROOT/'warm/warm.py'),'--host',a.host,'--repo',str(a.repo),'--output',str(a.output/identity),'--attempt',identity,'--workflow','docker','--queue-timeout-seconds','300','--docker-request',json.dumps(spec)]
        children.append(subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,start_new_session=True))
        if i==0:
            until(lambda:'FIFO_START 0' in state(identity).get('stdout',''))
        # Remaining submissions prepare independently; ticket order is measured remotely.
    for identity in ids[1:]:until(lambda identity=identity:registered(identity))
    snapshot=remote("python3 -c \"import sqlite3,json; from pathlib import Path; d=sqlite3.connect(Path.home()/'pandora-warm/admission.sqlite3'); print(json.dumps(d.execute('SELECT ticket,attempt,phase FROM requests ORDER BY ticket').fetchall()))\"")
    (a.output/'queue-before-faults.json').write_text(snapshot)
    print('All twelve requests admitted; injecting faults',flush=True)
    # A lost local client cannot dequeue its accepted remote run.
    os.killpg(children[2].pid,signal.SIGKILL);children[2].wait(timeout=10)
    query(a.host,ids[3],'cancel')
    for i in [5,0]:
        remote('sudo systemctl kill --kill-whom=main --signal=SIGKILL pandora-worker-'+ids[i]+'.service')
        until(lambda i=i:'"cleanup_verified": true' in remote('cat ~/pandora-warm/runs/'+ids[i]+'/admission-cleanup.json 2>/dev/null || true'))
        if children[i].poll() is None:
            os.killpg(children[i].pid,signal.SIGKILL);children[i].wait(timeout=10)
        assert not state(ids[i]).get('cleanup_verified'),'Worker death invented terminal success'
    # Recovery reads accepted metadata, despite a different environment default.
    retrylog=(a.output/'2-retry.log').open('w');handles.append(retrylog)
    recovered=subprocess.Popen([sys.executable,'-B',str(ROOT/'warm/transport.py'),a.host,str(a.output/ids[2])],stdout=retrylog,stderr=subprocess.STDOUT,env=dict(os.environ,PANDORA_QUEUE_TIMEOUT_SECONDS='1'))
    results=[]
    for i,identity in enumerate(ids):
        if i in [0,5]:
            results.append({'index':i,'attempt':identity,'worker_death':True,'terminal_missing':True,'cleanup_receipt':True})
            continue
        child=recovered if i==2 else children[i]
        code=child.wait(timeout=180)
        expected=130 if i==3 else 75 if i==4 else 0
        assert code==expected,(i,code,expected)
        path=a.output/identity
        metadata=json.loads((path/'submission.json').read_text())
        assert metadata['queue_timeout_seconds']==(2 if i==4 else 120)
        terminal=json.loads((path/'terminal.json').read_text())
        assert terminal['cleanup_verified'] and terminal['exit_code']==expected
        metrics=json.loads((path/'metrics.json').read_text()) if (path/'metrics.json').exists() else None
        results.append({'index':i,'attempt':identity,'exit_code':code,'terminal':terminal,'metrics':metrics,'queue_timeout_seconds':metadata['queue_timeout_seconds'],'recovered':i==2})
        query(a.host,identity,'release')
    executed=sorted([r for r in results if r.get('exit_code')==0],key=lambda r:r['metrics']['queue_ticket'])
    starts=[]
    for r in executed:
        text=(a.output/r['attempt']/'stdout.log').read_text()
        line=next(line for line in text.splitlines() if line.startswith('FIFO_START '))
        starts.append(int(line.split()[-1]))
    assert starts==sorted(starts),'Execution violated ticket order'
    assert len(executed)==8
    (a.output/'results.json').write_text(json.dumps({'requests':results,'execution_order':[r['index'] for r in executed],'strict_ticket_order':True},indent=2)+'\n')
    success=True
    print('FIFO order, deadline, cancellation, dead heads, and original-attempt recovery verified',flush=True)
finally:
    if not success:
        for identity in ids:
            try: query(a.host, identity, 'cancel')
            except Exception as error: print(f'Cleanup unresolved for {identity}: {error}', file=sys.stderr)
    if recovered is not None and recovered.poll() is None:
        recovered.terminate(); recovered.wait(timeout=15)
    for child in children:
        if child.poll() is None:
            # A failed probe cancels only its own accepted attempts, then stops followers.
            index=children.index(child)
            try:query(a.host,ids[index],'cancel')
            finally:os.killpg(child.pid,signal.SIGTERM);child.wait(timeout=15)
    for handle in handles:handle.close()
