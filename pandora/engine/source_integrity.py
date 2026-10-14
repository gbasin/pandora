"""Guest-side source verification executed by the engine, outside repository code."""
SCRIPT = r'''
import hashlib,json,os,re,stat,subprocess,sys
root=sys.argv[1]
entries=json.load(sys.stdin)['entries']
def blob(data,oid):
    algorithm='sha1' if len(oid)==40 else 'sha256'
    return hashlib.new(algorithm,b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
try:
    requests=[]
    contents=[]
    for path,(mode,kind,oid) in entries.items():
        if kind=='tree':
            if not stat.S_ISDIR(os.lstat(os.path.join(root,path)).st_mode):
                raise ValueError('directory changed')
            continue
        if kind!='blob':
            raise ValueError('gitlink')
        target=os.path.join(root,path)
        info=os.lstat(target)
        if mode=='120000':
            if not stat.S_ISLNK(info.st_mode):
                raise ValueError('symlink mode changed')
            data=os.fsencode(os.readlink(target))
        else:
            if not stat.S_ISREG(info.st_mode) or ('100755' if info.st_mode & 0o111 else '100644')!=mode:
                raise ValueError('file mode changed')
            with open(target,'rb') as stream:
                data=stream.read()
        if blob(data,oid)!=oid:
            if mode=='120000':
                raise ValueError('symlink changed')
            requests.append(oid)
            contents.append((hashlib.sha256(data).hexdigest(),len(data)))
    if requests:
        result=subprocess.run(['git','-C',root,'cat-file','--batch'],input=('\n'.join(requests)+'\n').encode(),stdout=subprocess.PIPE,check=True)
        raw=result.stdout
        for oid, actual in zip(requests,contents):
            header,raw=raw.split(b'\n',1)
            identity,kind,size=header.split()
            size=int(size)
            pointer,raw=raw[:size],raw[size+1:]
            if identity.decode()!=oid or kind!=b'blob' or blob(pointer,oid)!=oid:
                raise ValueError('invalid source object')
            match=re.fullmatch(rb'version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n',pointer)
            if not match or actual!=(match[1].decode(),int(match[2])):
                raise ValueError('source content changed')
    extras=subprocess.check_output(['git','-C',root,'ls-files','--cached','--others','--exclude-standard','-z']).split(b'\0')
    if any(path and path.decode() not in entries and path!=b'tmp/pandora-execution-report.json' for path in extras):
        raise ValueError('new source files')
    print('verified')
except Exception:
    print('unverified')
'''

CLEAR_SCRIPT = r'''
import os,stat,sys
root=os.path.realpath(sys.argv[1])
folder=os.path.join(root,'tmp')
if os.path.lexists(folder) and (os.path.islink(folder) or not os.path.isdir(folder)):
    raise SystemExit(1)
path=os.path.join(folder,'pandora-execution-report.json')
if os.path.lexists(path):
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise SystemExit(1)
    os.unlink(path)
print('cleared')
'''
