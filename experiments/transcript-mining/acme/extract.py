import json, os, glob, re, sys, collections
def clip(s, head=5000, tail=5000):
    if s is None: return ""
    if len(s)<=head+tail: return s
    return s[:head]+"\n...[CLIPPED %d chars]...\n"%(len(s)-head-tail)+s[-tail:]

ROOT = os.path.expanduser("~/.claude/projects")
dirs = sorted(d for d in glob.glob(os.path.join(ROOT, "*acme*")) if "pandora" not in d.lower())
files = []
for d in dirs: files += sorted(glob.glob(os.path.join(d, "*.jsonl")))

VAL = re.compile(r"(pnpm\s+(run\s+)?(validate|check|test|typecheck|lint|build|journey|e2e)|npm\s+(run\s+)?(test|build|lint)|turbo\s+run|vitest|playwright\s+test|npx\s+playwright|tsc\s|tsc$|eslint|pnpm\s+-w|pueue\s+(status|log|follow|wait)|^\s*make\s+(test|check)|jest|pytest)", re.I)

def norm(cmd):
    return re.sub(r"\s+", " ", cmd.strip())

out = open("events.jsonl","w")
seen = set()
nfiles=0
stats=collections.Counter()
for f in files:
    nfiles+=1
    with open(f, errors="replace") as fh:
        pending = {}   # tool_use_id -> info
        for lineno, line in enumerate(fh, 1):
            line=line.strip()
            if not line: continue
            try: o=json.loads(line)
            except Exception: continue
            uid=o.get("uuid")
            key=(o.get("sessionId") or "", uid)
            t=o.get("type")
            if t=="assistant":
                if uid in seen: 
                    stats["dup_assistant"]+=1
                m=o.get("message",{})
                u=m.get("usage") or {}
                rec={"k":"a","f":f,"ln":lineno,"uuid":uid,"ts":o.get("timestamp"),
                     "sid":o.get("sessionId"),"side":bool(o.get("isSidechain")),
                     "model":m.get("model"),
                     "in":u.get("input_tokens",0) or 0,"out":u.get("output_tokens",0) or 0,
                     "cr":u.get("cache_read_input_tokens",0) or 0,"cc":u.get("cache_creation_input_tokens",0) or 0,
                     "br":o.get("gitBranch"),"cwd":o.get("cwd")}
                texts=[]; tools=[]
                cont=m.get("content")
                if isinstance(cont,list):
                    for c in cont:
                        if not isinstance(c,dict): continue
                        if c.get("type")=="text": texts.append(c.get("text",""))
                        elif c.get("type")=="thinking": texts.append(c.get("thinking",""))
                        elif c.get("type")=="tool_use":
                            tools.append({"id":c.get("id"),"name":c.get("name"),
                                          "cmd": (c.get("input") or {}).get("command") if c.get("name")=="Bash" else None,
                                          "desc": (c.get("input") or {}).get("description") if c.get("name")=="Bash" else None})
                rec["text"]="\n".join(texts)[:6000]
                rec["tools"]=tools
                out.write(json.dumps(rec)+"\n"); stats["a"]+=1
            elif t=="user":
                m=o.get("message",{})
                cont=m.get("content")
                trs=[]
                if isinstance(cont,list):
                    for c in cont:
                        if isinstance(c,dict) and c.get("type")=="tool_result":
                            body=c.get("content")
                            if isinstance(body,list):
                                body="".join(x.get("text","") for x in body if isinstance(x,dict))
                            elif not isinstance(body,str): body=json.dumps(body)
                            trs.append({"id":c.get("tool_use_id"),"err":bool(c.get("is_error")),"body":clip(body)})
                tur=o.get("toolUseResult")
                extra={}
                if isinstance(tur,dict):
                    extra={"stdout":clip(tur.get("stdout") or ""),"stderr":clip(tur.get("stderr") or ""),
                           "interrupted":tur.get("interrupted"),"rc":tur.get("returnCode") if "returnCode" in tur else None}
                if trs:
                    out.write(json.dumps({"k":"r","f":f,"ln":lineno,"uuid":uid,"ts":o.get("timestamp"),
                                          "sid":o.get("sessionId"),"side":bool(o.get("isSidechain")),
                                          "res":trs,"extra":extra})+"\n"); stats["r"]+=1
                elif isinstance(cont,str) or isinstance(cont,list):
                    txt=cont if isinstance(cont,str) else "".join(c.get("text","") for c in cont if isinstance(c,dict) and c.get("type")=="text")
                    out.write(json.dumps({"k":"u","f":f,"ln":lineno,"uuid":uid,"ts":o.get("timestamp"),
                                          "sid":o.get("sessionId"),"side":bool(o.get("isSidechain")),
                                          "text":txt[:3000]})+"\n"); stats["u"]+=1
out.close()
print(nfiles, dict(stats))
