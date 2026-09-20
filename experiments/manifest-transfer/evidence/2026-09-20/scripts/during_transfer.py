"""Deterministic mutation while rsync is actively receiving a throttled file."""
from pathlib import Path
import json
import os
import subprocess
import time
import uuid
from probe import capture, remote, run, verify_remote, SSH_OPTIONS, HOST
p=Path(__file__).resolve().parent
repo=p/'during-repo'
repo.mkdir()
run(['git','init','-q',str(repo)])
(repo/'large').write_bytes(os.urandom(4*1024*1024))
(repo/'source').write_text('before')
inventory,manifest=capture(repo)
dest='/home/ubuntu/pandora-warm/.poc-stress-test/'+uuid.uuid4().hex
remote('from pathlib import Path\nPath('+repr(dest)+').mkdir(parents=True)\n')
try:
    with (p/'during-transfer.log').open('wb') as log:
        proc=subprocess.Popen(['rsync','-lpcd','--from0','--files-from=-','--bwlimit=512',
            '-e','ssh '+' '.join(SSH_OPTIONS),str(repo)+'/',HOST+':'+dest+'/'],
            stdin=subprocess.PIPE,stdout=log,stderr=log)
        proc.stdin.write(b''.join(r['path'].encode()+b'\0' for r in manifest));proc.stdin.close()
        deadline=time.monotonic()+20
        receiving=False
        while time.monotonic()<deadline and proc.poll() is None:
            state=remote('from pathlib import Path\np=Path('+repr(dest)+')\nprint(any(x.is_file() and 0<x.stat().st_size<4194304 for x in p.iterdir()))\n').strip()
            if state=='True':
                receiving=True
                break
            time.sleep(.1)
        assert receiving and proc.poll() is None, 'Did not observe active transfer'
        (repo/'source').write_text('after!')
        code=proc.wait(timeout=30)
        rejected=code!=0
        if not rejected:
            try:
                verify_remote(dest,manifest)
                rejected=capture(repo)!=(inventory,manifest)
            except subprocess.CalledProcessError:
                rejected=True
        assert rejected
        (p/'during-transfer.json').write_text(json.dumps(dict(observed_active_receiver=True,rsync_exit=code,capture_rejected=True,worker_started=False),indent=2)+'\n')
finally:
    if 'proc' in globals() and proc.poll() is None:
        proc.terminate();proc.wait(timeout=10)
    remote('import shutil\nshutil.rmtree('+repr(dest)+')\n')
