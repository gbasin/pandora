import json, collections, os, sys, re
d=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,d)

# rebuild session event lists (deduped, in order) for turn/token accounting
sessions=collections.defaultdict(list)
seen=set()
for line in open(d+"/events.jsonl"):
    o=json.loads(line)
    if o["uuid"] in seen: continue
    seen.add(o["uuid"]); sessions[o["f"]].append(o)

runs=collections.defaultdict(list)
for line in open(d+"/runs2.jsonl"):
    r=json.loads(line); runs[r["f"]].append(r)

# session totals
tot=dict(turns=0, out=0, inp=0, cr=0, cc=0)
sess_tot={}
for f,evs in sessions.items():
    t=dict(turns=0,out=0,inp=0,cr=0,cc=0)
    for e in evs:
        if e["k"]=="a":
            t["turns"]+=1; t["out"]+=e["out"]; t["inp"]+=e["in"]; t["cr"]+=e["cr"]; t["cc"]+=e["cc"]
    sess_tot[f]=t
    for k in tot: tot[k]+=t[k]
print("CORPUS TOTALS", tot, "sessions", len(sessions))

eps=[]
for f,evs in sessions.items():
    rl=[r for r in runs[f] if r["prim"]!="sleep" or r["st"]=="red"]
    rl.sort(key=lambda r: r["idx"])
    open_eps={}
    for r in rl:
        k=r["prim"]
        if r["st"]=="red":
            if k not in open_eps:
                open_eps[k]={"f":f,"kind":k,"start_idx":r["idx"],"start_ln":r["ln"],
                              "start_ts":r["ts"],"cmd":r["cmd"],"body":r["body"],
                              "fails":r["fails"],"envs":r["envs"],"reds":1,"runs":[r]}
            else:
                open_eps[k]["reds"]+=1; open_eps[k]["runs"].append(r)
        elif r["st"]=="green" and k in open_eps:
            e=open_eps.pop(k)
            e["end_idx"]=r["idx"]; e["end_ln"]=r["ln"]; e["end_ts"]=r["ts"]; e["closed"]=True
            e["runs"].append(r)
            eps.append(e)
        elif k in open_eps:
            open_eps[k]["runs"].append(r)
    last=len(evs)-1
    for k,e in open_eps.items():
        e["end_idx"]=last; e["end_ln"]=evs[last]["ln"] if evs else 0
        e["end_ts"]=evs[last]["ts"] if evs else None; e["closed"]=False
        eps.append(e)

# accounting: turns/tokens in [start_idx+1 .. end_idx]
for e in eps:
    evs=sessions[e["f"]]
    turns=0; out=0; inp=0; cr=0; cc=0; texts=[]; cmds=[]
    for i in range(e["start_idx"], min(e["end_idx"],len(evs)-1)+1):
        ev=evs[i]
        if ev["k"]=="a":
            turns+=1; out+=ev["out"]; inp+=ev["in"]; cr+=ev["cr"]; cc+=ev["cc"]
            if ev.get("text"): texts.append(ev["text"])
            for t in ev["tools"]:
                if t["name"]=="Bash" and t["cmd"]: cmds.append(t["cmd"][:400])
        elif ev["k"]=="u":
            texts.append("[USER] "+ev.get("text","")[:800])
    e["turns"]=turns; e["out"]=out; e["inp"]=inp; e["cr"]=cr; e["cc"]=cc
    e["ntext"]=len(texts)
    e["text"]="\n".join(texts)[:60000]
    e["cmds"]=cmds[:200]
    e["sid"]=evs[e["start_idx"]]["sid"] if evs else None
    e["side"]=evs[e["start_idx"]]["side"] if evs else None
    e["runs"]=[{k:r[k] for k in ("idx","ln","st","prim","cmd","fails","envs","exit")} for r in e["runs"]]

eps.sort(key=lambda e:-e["out"])
with open(d+"/episodes.jsonl","w") as fh:
    for e in eps: fh.write(json.dumps(e)+"\n")
print("episodes", len(eps))
print("closed", sum(1 for e in eps if e["closed"]))
print("ep turns", sum(e["turns"] for e in eps), "out", sum(e["out"] for e in eps), "cr", sum(e["cr"] for e in eps))
print("median turns", sorted(e["turns"] for e in eps)[len(eps)//2])
