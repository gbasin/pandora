import json, os, glob, datetime, collections

ROOT = os.path.expanduser("~/.claude/projects")
dirs = [d for d in glob.glob(os.path.join(ROOT, "*eichler*")) if "pandora" not in d.lower()]
files = []
for d in dirs:
    files += glob.glob(os.path.join(d, "*.jsonl"))
print("dirs", len(dirs), "files", len(files))

stats = dict(lines=0, assistant=0, user=0, tool_use=0, inp=0, out=0, cr=0, cc=0)
dates=[]
models=collections.Counter()
bad=0
sizes=[]
for f in files:
    sizes.append((os.path.getsize(f), f))
    with open(f, errors="replace") as fh:
        for line in fh:
            line=line.strip()
            if not line: continue
            stats["lines"]+=1
            try: o=json.loads(line)
            except Exception: bad+=1; continue
            ts=o.get("timestamp")
            if ts: dates.append(ts[:10])
            t=o.get("type")
            if t=="assistant":
                stats["assistant"]+=1
                m=o.get("message",{})
                models[m.get("model")]+=1
                u=m.get("usage") or {}
                stats["inp"]+=u.get("input_tokens",0) or 0
                stats["out"]+=u.get("output_tokens",0) or 0
                stats["cr"]+=u.get("cache_read_input_tokens",0) or 0
                stats["cc"]+=u.get("cache_creation_input_tokens",0) or 0
                for c in m.get("content",[]) if isinstance(m.get("content"),list) else []:
                    if isinstance(c,dict) and c.get("type")=="tool_use": stats["tool_use"]+=1
            elif t=="user":
                stats["user"]+=1
print(json.dumps(stats, indent=1))
print("bad", bad)
print("date range", min(dates), max(dates), "distinct days", len(set(dates)))
print("models", models.most_common())
sizes.sort(reverse=True)
print("total MB", sum(s for s,_ in sizes)/1e6)
for s,f in sizes[:5]: print(round(s/1e6,1), f)
