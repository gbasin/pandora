import json, re, collections, os
d=os.path.dirname(os.path.abspath(__file__))
PRE=re.compile(r"(pre-?existing (failure|fail|red|break)|unrelated to (my|our|this|the) (change|diff|edit|work|PR)|already fail(s|ing) on (main|the base|origin)|(also )?fails on main|not caused by (my|this|our)|red before my change|failing on main too|exists on main|present on main( as well)?)", re.I)
FLK=re.compile(r"(\bflake\b|\bflaky\b|flakiness|transient(ly)? fail|intermittent(ly)? fail|passed on (a )?(re-?run|retry|second run|rerun)|re-?ran.{0,30}(it )?pass|known (flake|intermittent))", re.I)
sessions=collections.defaultdict(list); seen=set()
pre=[]; flk=[]
nA=0
for line in open(d+"/events.jsonl"):
    o=json.loads(line)
    if o["uuid"] in seen: continue
    seen.add(o["uuid"])
    if o["k"]!="a": continue
    nA+=1
    t=o.get("text") or ""
    if not t: continue
    for rx,acc in ((PRE,pre),(FLK,flk)):
        m=rx.search(t)
        if m:
            i=max(0,m.start()-140); acc.append((o["f"],o["ln"],t[i:m.end()+160].replace("\n"," ")))
print("CLAUDE assistant turns",nA)
print("turns mentioning pre-existing/unrelated:",len(pre),"sessions:",len(set(x[0] for x in pre)))
print("turns mentioning flake/transient/passed-on-rerun:",len(flk),"sessions:",len(set(x[0] for x in flk)))
json.dump({"pre":pre,"flk":flk}, open(d+"/phrases.json","w"))
for x in pre[:10]: print("  PRE",x[0].split("/")[-1][:10],x[1],"|",x[2][:230])
print()
for x in flk[:10]: print("  FLK",x[0].split("/")[-1][:10],x[1],"|",x[2][:230])
