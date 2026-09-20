"""Diagnostic only: atomic directory exchange preserves bytes, not path intent."""
import ctypes, json, os, platform, tempfile
from pathlib import Path
libc=ctypes.CDLL(None,use_errno=True)
def exchange(a,b):
    if platform.system()=='Darwin':
        rc=libc.renamex_np(os.fsencode(a),os.fsencode(b),2)
    else:
        rc=libc.renameat2(-100,os.fsencode(a),-100,os.fsencode(b),2)
    if rc: raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
results=[]
for scenario in ['unchanged','edit-before-exchange','open-writer-after-exchange','crash-after-exchange']:
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp); live=root/'dist'; staged=root/'retained-generation'
        live.mkdir(); staged.mkdir()
        (live/'out').write_text('baseline'); (staged/'out').write_text('remote')
        fd=(live/'out').open('r+')
        assert (live/'out').read_text()=='baseline'
        if scenario=='edit-before-exchange': (live/'out').write_text('local edit')
        exchange(live,staged)
        if scenario=='open-writer-after-exchange':
            fd.seek(0);fd.write('late local edit');fd.truncate();fd.flush()
        fd.close()
        wanted={'edit-before-exchange':'local edit','open-writer-after-exchange':'late local edit'}.get(scenario,'baseline')
        assert (staged/'out').read_text()==wanted
        assert (live/'out').read_text()=='remote'
        results.append(dict(scenario=scenario,canonical=(live/'out').read_text(),retained=(staged/'out').read_text(),local_edit_preserved_in_place=not scenario.startswith(('edit','open'))))
print(json.dumps({'platform':platform.system(),'results':results,'conclusion':'Retained old inode preserves edits, but conflict stays outside canonical path. Not a complete promotion protocol.'},indent=2))
