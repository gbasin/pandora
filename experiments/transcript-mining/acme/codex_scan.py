import json, os, glob
root=os.path.expanduser("~/.codex/sessions/2026")
files=glob.glob(os.path.join(root,"**","*.jsonl"), recursive=True)
hits=[]
for f in files:
    try:
        with open(f, errors="replace") as fh:
            for i,line in enumerate(fh):
                if i>5: break
                if "Code/acme" in line:
                    try: o=json.loads(line)
                    except: continue
                    cwd=json.dumps(o)
                    hits.append((f,line[:400]))
                    break
    except Exception as e: pass
print(len(files), "codex files;", len(hits), "mention Code/acme in header")
for f,l in hits[:10]: print(f); print("  ", l[:250])
