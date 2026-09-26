import json, re, collections, os, sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from cmdparse import analyze

PRIO = ["validate","journey","playwright","vitest","test","nodetest","check","typecheck","build","turbo","lint","format","pueue","sleep"]

sessions = collections.defaultdict(list)
seen=set()
for line in open("events.jsonl"):
    o=json.loads(line)
    if o["uuid"] in seen: continue
    seen.add(o["uuid"])
    sessions[o["f"]].append(o)

out=open("runs.jsonl","w")
n=0
for f, evs in sessions.items():
    res={}
    for e in evs:
        if e["k"]=="r":
            for r in e["res"]: res[r["id"]]=(e,r)
    for idx,e in enumerate(evs):
        if e["k"]!="a": continue
        for t in e["tools"]:
            if t["name"]!="Bash" or not t["cmd"]: continue
            h=analyze(t["cmd"])
            if not h: continue
            kinds=[k for k,_ in h]
            prim=min(kinds, key=lambda k: PRIO.index(k) if k in PRIO else 99)
            ent=res.get(t["id"])
            body=""; err=None; interrupted=None
            if ent:
                re_,r_=ent
                body=r_["body"]; err=r_["err"]
                ex=re_.get("extra") or {}
                interrupted=ex.get("interrupted")
                if not body: body=(ex.get("stdout") or "")+("\n"+ex.get("stderr") if ex.get("stderr") else "")
            rec={"f":f,"ln":e["ln"],"idx":idx,"uuid":e["uuid"],"ts":e["ts"],"sid":e["sid"],"side":e["side"],
                 "kinds":sorted(set(kinds)),"prim":prim,"args":[a for _,a in h][:6],
                 "cmd":t["cmd"][:2000],"err":err,"interrupted":interrupted,"body":body[:6000],
                 "has_res":ent is not None}
            out.write(json.dumps(rec)+"\n"); n+=1
out.close()
print("runs",n)
