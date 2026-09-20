"""POC: verified artifact directories, atomic no-clobber publish, retry recovery.
Exercises process-exit windows, not power loss or arbitrary hostile writers.
"""
import ctypes,hashlib,json,os,platform,tempfile
from pathlib import Path
libc=ctypes.CDLL(None,use_errno=True)
def no_replace(source,destination):
    if platform.system()=='Darwin':rc=libc.renamex_np(os.fsencode(source),os.fsencode(destination),4)
    else:rc=libc.renameat2(-100,os.fsencode(source),-100,os.fsencode(destination),1)
    if rc:raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
def verify(directory,manifest):
    if directory.is_symlink() or not directory.is_dir():raise ValueError('Invalid artifact directory')
    if {p.name for p in directory.iterdir()} != set(manifest):raise ValueError('Unexpected artifact set')
    for name,want in manifest.items():
        p=directory/name
        if '/' in name or name in {'.','..'} or p.is_symlink() or not p.is_file():raise ValueError('Unsafe artifact')
        if hashlib.sha256(p.read_bytes()).hexdigest()!=want:raise ValueError('Artifact digest mismatch')
def recover(stage,destination,manifest):
    if destination.exists():
        verify(destination,manifest)
        return 'recovered'
    verify(stage,manifest)
    try:no_replace(stage,destination)
    except FileExistsError:
        verify(destination,manifest)
        return 'recovered'
    return 'published'
results=[]
for crash in ['before-publish','after-publish']:
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);stage=root/'download';destination=root/'run-123'
        stage.mkdir();(stage/'output').write_bytes(b'verified output')
        manifest={'output':hashlib.sha256(b'verified output').hexdigest()}
        pid=os.fork()
        if pid==0:
            verify(stage,manifest)
            if crash=='after-publish':no_replace(stage,destination)
            os._exit(23)
        assert os.waitpid(pid,0)[1]==23<<8
        outcome=recover(stage,destination,manifest);verify(destination,manifest)
        results.append({'window':crash,'retry':outcome,'correct_bytes':True})
for bad in ['different-existing-result','symlink']:
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);stage=root/'download';destination=root/'run-123';stage.mkdir()
        (stage/'output').write_bytes(b'verified output')
        manifest={'output':hashlib.sha256(b'verified output').hexdigest()}
        if bad=='different-existing-result':
            destination.mkdir();(destination/'output').write_bytes(b'other run')
        else:
            (stage/'output').unlink();(stage/'output').symlink_to('/etc/hosts')
        try:recover(stage,destination,manifest)
        except ValueError:results.append({'case':bad,'rejected':True})
        else:raise AssertionError('Invalid evidence accepted')
        if bad=='different-existing-result':assert (destination/'output').read_bytes()==b'other run'
print(json.dumps({'platform':platform.system(),'results':results,'scope':'Run artifact publication only; no workspace writeback, power-loss durability, or concurrent-writer guarantee'},indent=2))
