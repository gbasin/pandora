import json, os, glob, collections
root=os.path.expanduser("~/.codex/sessions/2026")
files=glob.glob(os.path.join(root,"**","*.jsonl"), recursive=True)
c=collections.Counter(); keep=[]
for f in files:
    try:
        with open(f, errors="replace") as fh:
            line=fh.readline()
        o=json.loads(line)
        if o.get("type")!="session_meta": continue
        cwd=o["payload"].get("cwd","")
    except Exception: continue
    if cwd.startswith("/Users/you/Code/acme"):
        c[cwd]+=1
        if "pandora" not in cwd.lower(): keep.append(f)
for k,v in c.most_common(40): print(v, k)
print("TOTAL acme:", sum(c.values()), "non-pandora files:", len(keep))
open("codex_files.txt","w").write("\n".join(keep))
sz=sum(os.path.getsize(f) for f in keep)
print("MB", sz/1e6)
