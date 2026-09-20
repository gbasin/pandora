"""Synthetic-only probe: can an ancestor swap send non-admitted bytes?"""
from pathlib import Path
import json
import uuid
import subprocess
from probe import capture, transfer, remote, verify_remote, run
p=Path(__file__).resolve().parent
repo=p/'ancestor-repo'; repo.mkdir()
run(['git','init','-q',str(repo)])
(repo/'folder').mkdir(); (repo/'folder'/'data').write_text('admitted')
external=p/'synthetic-external';external.mkdir();(external/'data').write_text('SYNTHETIC-NOT-ADMITTED')
_,manifest=capture(repo)
(repo/'folder'/'data').unlink();(repo/'folder').rmdir();(repo/'folder').symlink_to(external)
dest='/home/ubuntu/pandora-warm/.poc-stress-test/'+uuid.uuid4().hex
remote('from pathlib import Path\nPath('+repr(dest)+').mkdir(parents=True)\n')
try:
    transfer_error=False
    try: transfer(repo,dest,manifest)
    except subprocess.CalledProcessError: transfer_error=True
    received=remote('from pathlib import Path\np=Path('+repr(dest)+')/"folder/data"\nprint(p.read_text() if p.is_file() else "absent")\n').strip()
    rejected=False
    try: verify_remote(dest,manifest)
    except subprocess.CalledProcessError: rejected=True
    result=dict(transfer_error=transfer_error,received=received,rejected_before_execution=rejected)
    (p/'ancestor-swap.json').write_text(json.dumps(result,indent=2)+'\n')
    print(result)
finally: remote('import shutil\nshutil.rmtree('+repr(dest)+')\n')
