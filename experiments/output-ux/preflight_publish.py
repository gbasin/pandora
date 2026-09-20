"""Live-VM delivery recovery and ordinary local consumer checks."""
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import urllib.request

source=Path(__file__).resolve().parent
results=[]
for crash in ['after_prepare','after_exchange',None]:
    with tempfile.TemporaryDirectory(prefix='pandora-publish-preflight-') as tmp:
        root=Path(tmp);fixture=root/'fixture'
        shutil.copytree(source/'fixture',fixture,ignore=shutil.ignore_patterns('node_modules','dist','.pandora-artifacts'))
        for name in ['bridge.py','publish.py']:shutil.copy(source/name,root/name)
        (fixture/'node_modules').mkdir();(fixture/'dist').mkdir()
        old=b'{"title":"Draft"}\n'
        (fixture/'dist/report.json').write_bytes(old);(fixture/'dist/obsolete').write_text('old artifact')
        schema=json.loads((fixture/'src/schema.json').read_text());schema['title']='Ready'
        (fixture/'src/schema.json').write_text(json.dumps(schema))
        env=dict(os.environ)
        if crash:env['PANDORA_PUBLISH_CRASH']=crash
        first=subprocess.run(['pnpm','build'],cwd=fixture,env=env,capture_output=True,text=True,timeout=90)
        if crash:
            assert first.returncode!=0
            second=subprocess.run(['pnpm','build'],cwd=fixture,capture_output=True,text=True,timeout=90)
            assert second.returncode==0,second.stdout+second.stderr
            assert 'no remote build submitted' in second.stdout
        else:assert first.returncode==0,first.stdout+first.stderr
        events=[json.loads(line) for line in (fixture/'.pandora-artifacts/remote-events.jsonl').read_text().splitlines()]
        assert len(events)==1
        data=(fixture/'dist/report.json').read_bytes();assert json.loads(data)['title']=='Ready'
        assert not (fixture/'dist/obsolete').exists()
        receipts=list((fixture/'.pandora-artifacts').glob('*/publication.json'));assert len(receipts)==1
        receipt=json.loads(receipts[0].read_text());backup=Path(receipt['retained_previous'])
        assert (backup/'report.json').read_bytes()==old and (backup/'obsolete').exists()
        node=subprocess.run(['node','-e',"console.log(JSON.parse(require('fs').readFileSync('dist/report.json')).title)"],cwd=fixture,capture_output=True,text=True,check=True)
        assert node.stdout.strip()=='Ready'
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(fixture)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/dist/report.json',timeout=5) as response:
                assert json.load(response)['title']=='Ready'
        finally:server.shutdown();server.server_close();thread.join()
        results.append({'crash':crash,'remote_builds':len(events),'old_generation_retained':True,'obsolete_output_removed':True,'node_read':'Ready','http_read':'Ready','output_sha256':hashlib.sha256(data).hexdigest(),'retry_without_remote_build':bool(crash)})
print(json.dumps(results,indent=2))
