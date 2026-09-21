import json, random, os, re
d=os.path.dirname(os.path.abspath(__file__))
eps=[json.loads(l) for l in open(d+"/episodes_c.jsonl")]
for i,e in enumerate(eps): e["id"]=i
random.seed(42)
by=lambda c:[e for e in eps if e["auto"]==c]
sel=[]
sel+=by("a_flaky"); sel+=by("c_rerun"); sel+=by("d_bisect"); sel+=by("e_wait")
for c,n in (("b_preexisting",12),("f_env",12),("g_real",18)):
    pool=by(c); pool_s=sorted(pool,key=lambda e:-e["out"])
    top=pool_s[:max(2,n//3)]
    rest=[e for e in pool if e not in top]
    sel+=top+random.sample(rest,min(n-len(top),len(rest)))
seen=set(); out=[]
for e in sel:
    if e["id"] in seen: continue
    seen.add(e["id"]); out.append(e)
print("sample size", len(out))
def rend(e, tmax=3800):
    s=[]
    s.append("### EP%d auto=%s kind=%s turns=%d out=%d closed=%s nreds=%d dup=%d wait=%s/%s"%(
        e["id"],e["auto"],e["kind"],e["turns"],e["out"],e["closed_by"],e["nreds"],e["dup_runs"],e.get("waitn"),e.get("ncmds")))
    s.append("FILE %s:%d  envs=%s fails=%s"%(e["f"],e["start_ln"],e["envs"],e["fails"]))
    s.append("CMD: "+" ".join(e["cmd"].split())[:400])
    s.append("OUT_TAIL: "+" | ".join(e["body"].splitlines()[-12:])[:900])
    s.append("CMDS_AFTER:")
    for c in e["cmds"][1:14]: s.append("  $ "+" ".join(c.split())[:170])
    s.append("TEXT: "+re.sub(r"\n{2,}","\n",e["text"])[:tmax])
    return "\n".join(s)
with open(d+"/sample.txt","w") as fh:
    for e in out: fh.write(rend(e)+"\n\n")
json.dump([e["id"] for e in out], open(d+"/sample_ids.json","w"))
print("chars", os.path.getsize(d+"/sample.txt"))
