import json, os, sys, re, collections
d=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,d)
from cmdparse import analyze
from outcome import outcome
fs=open(d+"/codex_files.txt").read().split()
stats=collections.Counter(); dates=[]
inp=out=cin=0
runs=[]
nass=0
for f in fs:
    pend={}
    try: fh=open(f,errors="replace")
    except: continue
    for line in fh:
        if '"timestamp"' not in line: continue
        # cheap prefilters
        need = ('custom_tool_call' in line or 'function_call' in line or 'token_usage_record' in line
                or '"token_count"' in line or '"assistant"' in line)
        if not need: continue
        try: o=json.loads(line)
        except: continue
        p=o.get("payload") or {}
        t=p.get("type")
        if o.get("type")=="token_usage_record":
            u=p.get("usage") or {}
            inp+=u.get("input_tokens",0) or 0; out+=u.get("output_tokens",0) or 0
            cin+=u.get("cached_input_tokens",0) or 0
            stats["turns"]+=1
            if o.get("timestamp"): dates.append(o["timestamp"][:10])
        elif t in ("custom_tool_call","function_call"):
            name=p.get("name"); raw=p.get("input") or p.get("arguments") or ""
            cmd=None
            if name in ("shell","exec","local_shell","container.exec"):
                try:
                    a=json.loads(raw) if isinstance(raw,str) and raw.strip().startswith("{") else None
                except: a=None
                if isinstance(a,dict):
                    cc=a.get("command")
                    cmd=" ".join(cc) if isinstance(cc,list) else cc
                elif isinstance(raw,str):
                    m=re.findall(r'cmd\s*:\s*"((?:[^"\\\\]|\\\\.)*)"', raw)
                    if m:
                        cmd="\n".join(x.encode().decode("unicode_escape","replace") for x in m)
                    else: cmd=raw
            if cmd:
                stats["bash"]+=1
                h=analyze(cmd)
                if any(k not in ("sleep","pueue") for k,_ in h):
                    pend[p.get("call_id")]= (f,cmd,h)
        elif t in ("custom_tool_call_output","function_call_output"):
            cid=p.get("call_id")
            if cid in pend:
                f_,cmd,h=pend.pop(cid)
                body=p.get("output")
                if isinstance(body,str):
                    try:
                        j=json.loads(body); body=j.get("output") if isinstance(j,dict) else body
                    except: pass
                body=body if isinstance(body,str) else json.dumps(body)
                st,fl,pa,en,ec=outcome(body[:6000], None)
                runs.append({"f":f_,"cmd":cmd[:600],"st":st,"fails":fl,"envs":en,"body":body[:1500]})
    fh.close()
print("codex sessions",len(fs),"turn records",stats["turns"],"bash",stats["bash"])
print("tokens input",inp,"cached_in",cin,"output",out)
if dates: print("date range",min(dates),max(dates),"days",len(set(dates)))
print("validation runs",len(runs),collections.Counter(r["st"] for r in runs))
json.dump(runs, open(d+"/codex_runs.json","w"))
