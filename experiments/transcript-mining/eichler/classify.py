import json, re, os, collections, sys
d=os.path.dirname(os.path.abspath(__file__))

RX = {
 "b_preexist": re.compile(r"(pre-?existing|pre-?exists|unrelated to (my|our|the|this) change|unrelated to the (edit|diff|work)|already fail(s|ing) on (main|base|origin)|also fails on main|fails on main too|not caused by (my|this|our)|baseline failure|same failure on (main|base)|exists on main|present on main|on main as well|red before my change|already red)", re.I),
 "b_verify": re.compile(r"(git (stash|worktree add|checkout) .*(main|origin/main)|git checkout (origin/)?main\b|git -C .* checkout (origin/)?main)", re.I),
 "a_flake": re.compile(r"(\bflak(e|y|iness)\b|transient(ly)? fail|intermittent|non-?deterministic|passed on (a )?(re-?run|retry|second run)|re-?ran? .* (and it )?pass|retry(ing)? .* pass|timing[- ]dependent|\brace\b.*(test|flake)|unstable test)", re.I),
 "d_bisect": re.compile(r"(bisect|revert(ing)? (my|the|that) (edit|change|hunk|commit)|git checkout -- |git restore |undo (my|the) (edit|change)|which (edit|change|hunk) broke|back out (the|my) change|selectively revert|narrow down which)", re.I),
 "e_wait": re.compile(r"(pueue (status|log|follow|wait)|sleep \d+|still running|wait(ing)? for .* (to finish|to complete)|poll(ing)?|tail -f|while .*running)", re.I),
 "f_env": re.compile(r"(docker (daemon|not running|isn't running)|colima|port \d+ (is )?(already )?in use|EADDRINUSE|node_modules|pnpm install|Cannot find module|command not found|playwright install|browser(s)? (not )?installed|Executable doesn't exist|stale (build|dist|cache)|ECONNREFUSED|postgres.*not (running|up)|db:up|rm -rf .*(dist|node_modules|\.turbo)|turbo cache)", re.I),
}

def norm_cmd(c):
    c=re.sub(r"\s+"," ",c.strip())
    return c

eps=[json.loads(l) for l in open(d+"/episodes.jsonl")]
# (c) identical rerun with no edits between: need event stream
sessions=collections.defaultdict(list); seen=set()
for line in open(d+"/events.jsonl"):
    o=json.loads(line)
    if o["uuid"] in seen: continue
    seen.add(o["uuid"]); sessions[o["f"]].append(o)

for e in eps:
    evs=sessions[e["f"]]
    prev={}; dup=0; dup_examples=[]
    edited_since=set()
    lastcmd=None
    seq=[]
    for i in range(e["si"], min(e["ei"],len(evs)-1)+1):
        ev=evs[i]
        if ev["k"]!="a": continue
        for t in ev["tools"]:
            if t["name"] in ("Edit","Write","NotebookEdit"): seq.append(("edit",None,i))
            elif t["name"]=="Bash" and t["cmd"]:
                c=norm_cmd(t["cmd"])
                mut = bool(re.search(r"(^|[;&|]\s*)(python3|perl|sed -i|git (apply|checkout|revert|stash|reset|cherry-pick)|cat >|tee |mv |cp |rm )", c)) or "<<" in c
                seq.append(("mut" if mut else "cmd", c, i))
    lastseen={}
    edits_after={}
    for j,(typ,c,i) in enumerate(seq):
        if typ in ("edit","mut"):
            lastseen={}  # any mutation invalidates "unchanged source"
            continue
        if c in lastseen:
            dup+=1
            if len(dup_examples)<3: dup_examples.append((lastseen[c], i, c[:200]))
        lastseen[c]=i
    e["dup_runs"]=dup; e["dup_examples"]=dup_examples

for e in eps:
    txt=e["text"]
    sig={k:bool(rx.search(txt)) for k,rx in RX.items()}
    sig["b_verify"]=sig["b_verify"] or bool(RX["b_verify"].search("\n".join(e["cmds"])))
    WAITC=re.compile(r"(^|[;&|]\s*)(sleep\s+\d|pueue\b|until\s|gh (run|pr) (watch|checks))|--watch|tail -f", re.I)
    waitn=sum(1 for c in e["cmds"] if WAITC.search(c))
    e["waitn"]=waitn; e["ncmds"]=len(e["cmds"])
    sig["e_wait"]= waitn>=3 and waitn >= 0.34*max(1,len(e["cmds"]))
    sig["f_env"]=sig["f_env"] or bool(e["envs"])
    e["sig"]=sig
    # precedence
    if sig["a_flake"]: c="a_flaky"
    elif sig["b_preexist"] or (sig["b_verify"]): c="b_preexisting"
    elif e["envs"] or (sig["f_env"] and e["kind"] not in ("sleep",)): c="f_env"
    elif sig["e_wait"]: c="e_wait"
    elif sig["d_bisect"]: c="d_bisect"
    elif e["dup_runs"]>0: c="c_rerun"
    else: c="g_real"
    e["auto"]=c
with open(d+"/episodes_c.jsonl","w") as fh:
    for e in eps: fh.write(json.dumps(e)+"\n")
cc=collections.Counter(e["auto"] for e in eps)
print(cc.most_common())
for k in cc:
    sub=[e for e in eps if e["auto"]==k]
    print(k, "n=",len(sub), "turns",sum(x["turns"] for x in sub), "out",sum(x["out"] for x in sub), "cr",sum(x["cr"] for x in sub))
print("dup_runs total", sum(e["dup_runs"] for e in eps), "eps with dup", sum(1 for e in eps if e["dup_runs"]))
