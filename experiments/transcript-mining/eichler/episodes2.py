import json, collections, os, sys, re
d=os.path.dirname(os.path.abspath(__file__))

sessions=collections.defaultdict(list); seen=set()
for line in open(d+"/events.jsonl"):
    o=json.loads(line)
    if o["uuid"] in seen: continue
    seen.add(o["uuid"]); sessions[o["f"]].append(o)
runs=collections.defaultdict(list)
for line in open(d+"/runs2.jsonl"):
    r=json.loads(line); runs[r["f"]].append(r)

MAXT=60
eps=[]
for f,evs in sessions.items():
    # user message indices (real user prompts, not tool results)
    user_idx=[i for i,e in enumerate(evs) if e["k"]=="u" and not (e.get("text","") or "").startswith("<")]
    rl=sorted(runs[f], key=lambda r:r["idx"])
    HARNESS=re.compile(r"<tool_use_error>|Refusing to run it|^Blocked:", re.M)
    reds=[r for r in rl if r["st"]=="red" and r["prim"] not in ("sleep","pueue") and not HARNESS.search(r["body"])]
    used=set()
    for r in reds:
        if r["idx"] in used: continue
        si=r["idx"]; k=r["prim"]
        # end candidates
        end=None
        for r2 in rl:
            if r2["idx"]>si and r2["prim"]==k and r2["st"]=="green": end=r2["idx"]; break
        nu=next((i for i in user_idx if i>si), None)
        cands=[x for x in (end, nu, si+MAXT, len(evs)-1) if x is not None]
        ei=min(cands)
        # absorb subsequent reds of same kind inside window
        inner=[x for x in reds if si<=x["idx"]<=ei and x["prim"]==k]
        for x in inner: used.add(x["idx"])
        eps.append({"f":f,"kind":k,"si":si,"ei":ei,"start_ln":r["ln"],"start_ts":r["ts"],
                    "cmd":r["cmd"],"body":r["body"],"fails":r["fails"],"envs":r["envs"],
                    "exit":r["exit"],"nreds":len(inner),
                    "closed_by":"green" if ei==end else ("user" if ei==nu else ("cap" if ei==si+MAXT else "eof")),
                    "runs":[{kk:x[kk] for kk in ("idx","ln","st","prim","cmd","fails","envs","exit")} for x in rl if si<=x["idx"]<=ei]})
# attribute turns non-overlapping: sort by si, assign each turn index to first episode
eps.sort(key=lambda e:(e["f"],e["si"]))
claimed=collections.defaultdict(set)
for e in eps:
    evs=sessions[e["f"]]
    turns=0;out=0;inp=0;cr=0;cc=0;texts=[];cmds=[];excl=0
    for i in range(e["si"], min(e["ei"],len(evs)-1)+1):
        ev=evs[i]
        if ev["k"]=="a":
            if i in claimed[e["f"]]: excl+=1
            else:
                claimed[e["f"]].add(i)
                turns+=1;out+=ev["out"];inp+=ev["in"];cr+=ev["cr"];cc+=ev["cc"]
            if ev.get("text"): texts.append(ev["text"])
            for t in ev["tools"]:
                if t["name"]=="Bash" and t["cmd"]: cmds.append(t["cmd"][:500])
        elif ev["k"]=="u":
            texts.append("[USER] "+(ev.get("text") or "")[:800])
    e.update(turns=turns,out=out,inp=inp,cr=cr,cc=cc,overlap_turns=excl,
             text="\n".join(texts)[:80000], cmds=cmds[:250],
             sid=evs[e["si"]]["sid"], side=evs[e["si"]]["side"],
             span=e["ei"]-e["si"]+1)
with open(d+"/episodes.jsonl","w") as fh:
    for e in eps: fh.write(json.dumps(e)+"\n")
import statistics
print("episodes",len(eps))
print("closed_by",collections.Counter(e["closed_by"] for e in eps))
print("kind",collections.Counter(e["kind"] for e in eps).most_common())
print("turns",sum(e["turns"] for e in eps),"out",sum(e["out"] for e in eps),"cr",sum(e["cr"] for e in eps),"cc",sum(e["cc"] for e in eps),"inp",sum(e["inp"] for e in eps))
t=[e["turns"] for e in eps]
print("median",statistics.median(t),"mean",round(statistics.mean(t),1),"p90",sorted(t)[int(.9*len(t))])
