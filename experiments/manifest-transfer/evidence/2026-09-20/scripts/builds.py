from pathlib import Path
import json
import subprocess
import sys
import time
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'experiments/warm'))
from transport import query
p=root/'.poc-stress-test'
spec=(p/'docker-spec.json').read_text()
for index in range(3):
    output=p/f'build-{index}'
    start=time.monotonic()
    result=subprocess.run(['python3','-B',str(p/'warm_probe.py'),'--host','ubuntu@WORKER','--repo','/Users/garybasin/Code/eichler/.worktrees/pandora-compiled-build','--output',str(output),'--workflow','docker','--docker-request',spec],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    (p/f'build-{index}.log').write_text(result.stdout)
    print(result.stdout[-1000:],flush=True)
    assert result.returncode==0,result.returncode
    metadata=json.loads((output/'submission.json').read_text())
    metadata['direct_cli_seconds']=time.monotonic()-start
    (output/'measurement.json').write_text(json.dumps(metadata,indent=2)+'\n')
    receipt=query('ubuntu@WORKER',metadata['attempt'],'release')
    assert receipt['cleanup_verified'] and receipt['exit_code']==0, receipt
    print(json.dumps(metadata),flush=True)
