import json, collections, re, os, sys, statistics
d=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,d)
from cmdparse import analyze, strip_noise

sessions=collections.defaultdict(list); seen=set()
for line in open(d+"/events.jsonl"):
    o=json.loads(line)
    if o["uuid"] in seen: continue
    seen.add(o["uuid"]); sessions[o["f"]].append(o)

# ---- (c) identical validation command rerun with no intervening mutation ----
MUT=re.compile(r"(^|[;&|]\s*)(python3?|perl|sed\s+-i|git\s+(apply|checkout|revert|stash|reset|cherry-pick|merge|rebase|pull)|cat\s*>|tee\s|mv\s|cp\s|rm\s|npx?\s+oxfmt|pnpm\s+(exec\s+)?oxfmt|pnpm\s+install|ln\s+-s)")
def normv(c):
    return re.sub(r"\s+"," ",c.strip())
dupn=0; dup_ex=[]; totval=0
for f,evs in sessions.items():
    lastseen={}
    for i,ev in enumerate(evs):
        if ev["k"]!="a": continue
        for t in ev["tools"]:
            if t["name"] in ("Edit","Write","NotebookEdit"): lastseen={}
            if t["name"]!="Bash" or not t["cmd"]: continue
            c=t["cmd"]
            h=analyze(c)
            isval = any(k not in ("sleep","pueue") for k,_ in h)
            if MUT.search(strip_noise(c)) or "<<" in c:
                lastseen={}
                if not isval: continue
            if not isval: continue
            totval+=1
            n=normv(c)
            if n in lastseen:
                dupn+=1
                if len(dup_ex)<25: dup_ex.append((f,evs[lastseen[n]]["ln"],ev["ln"],n[:200]))
            lastseen[n]=i
print("validation bash calls:",totval,"identical reruns with no intervening mutation:",dupn)
for e in dup_ex[:12]: print("   ",e[0].split("/")[-1][:10],e[1],"->",e[2],"|",e[3][:140])

# ---- (e) waiting/polling ----
WAIT=re.compile(r"(^|[;&|]\s*)(sleep\s+\d|pueue\b|until\s|while\s+(true|!)|gh\s+(run|pr)\s+(watch|checks))|--watch\b|tail\s+-f|gh run watch", re.I)
wait_calls=0; wait_turns=set(); wait_out=0; harness_block=0
for f,evs in sessions.items():
    for i,ev in enumerate(evs):
        if ev["k"]!="a": continue
        hit=False
        for t in ev["tools"]:
            if t["name"]=="Bash" and t["cmd"] and WAIT.search(strip_noise(t["cmd"])):
                wait_calls+=1; hit=True
        if hit:
            wait_turns.add((f,i)); wait_out+=ev["out"]
print("waiting/polling bash calls:",wait_calls,"turns containing one:",len(wait_turns),"output tokens on those turns:",wait_out)

# harness-blocked sleeps
nb=0
for line in open(d+"/runs2.jsonl"):
    r=json.loads(line)
    if "<tool_use_error>" in r["body"] and "sleep" in r["cmd"][:200]: nb+=1
print("harness-blocked sleep chains:",nb)

# ---- episode cost distributions ----
eps=[json.loads(l) for l in open(d+"/episodes_c.jsonl")]
g=[e for e in eps if e["closed_by"]=="green"]
print("closed-by-green episodes:",len(g))
t=sorted(e["turns"] for e in g)
print("  turns median",statistics.median(t),"mean",round(statistics.mean(t),1),"p90",t[int(.9*len(t))],"total",sum(t))
print("  out tokens total",sum(e["out"] for e in g))
for k in ("user","cap","eof"):
    s=[e for e in eps if e["closed_by"]==k]
    print(" ",k,len(s),"turns",sum(e["turns"] for e in s),"out",sum(e["out"] for e in s))

# top 10 by output tokens
eps.sort(key=lambda e:-e["out"])
print("\nTOP 10 BY OUTPUT TOKENS")
for e in eps[:12]:
    print(f'  EP? {e["auto"]:14s} {e["kind"]:10s} turns={e["turns"]:3d} out={e["out"]:7d} closed={e["closed_by"]:6s} {e["f"]}:{e["start_ln"]}')
