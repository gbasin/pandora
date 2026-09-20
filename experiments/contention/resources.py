"""Bounded resource sampling; never manages agent lifecycle."""
import argparse
import json
from pathlib import Path
import subprocess
import time

p=argparse.ArgumentParser()
p.add_argument('--output',type=Path,required=True)
p.add_argument('--stop',type=Path,required=True)
p.add_argument('--host',required=True)
a=p.parse_args()
deadline=time.monotonic()+900
with a.output.open('w') as stream:
    while time.monotonic()<deadline and not a.stop.exists():
        record={'time':time.time()}
        for label,command in [
            ('local_memory',['memory_pressure','-Q']),
            ('remote',['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',a.host,
                'sudo docker ps --format "{{.Names}}"; free -m'])]:
            try:
                result=subprocess.run(command,capture_output=True,text=True,timeout=12)
                record[label]={'exit':result.returncode,'stdout':result.stdout,'stderr':result.stderr}
            except subprocess.TimeoutExpired:
                record[label]={'timeout':True}
        stream.write(json.dumps(record)+'\n');stream.flush()
        time.sleep(3)
