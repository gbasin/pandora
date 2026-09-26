import json, re, collections, os

VAL_PATTERNS = [
 (r"pnpm\s+(run\s+)?validate\b", "validate"),
 (r"tools/validate\.mjs", "validate"),
 (r"pnpm\s+(run\s+)?check\b", "check"),
 (r"pnpm\s+(run\s+)?check:", "check"),
 (r"pnpm\s+(run\s+)?test\b", "test"),
 (r"pnpm\s+(run\s+)?test:", "test"),
 (r"pnpm\s+(run\s+)?typecheck\b", "typecheck"),
 (r"pnpm\s+(run\s+)?lint\b", "lint"),
 (r"pnpm\s+(run\s+)?build\b", "build"),
 (r"pnpm\s+(run\s+)?journeys?\b", "journey"),
 (r"\bvitest\b", "vitest"),
 (r"playwright\s+test", "playwright"),
 (r"\boxlint\b", "lint"),
 (r"\boxfmt\b", "format"),
 (r"\btsc\b(\s|$)", "typecheck"),
 (r"node\s+--test", "nodetest"),
 (r"\bturbo\s+run\b", "turbo"),
]
VAL_RE = [(re.compile(p, re.I), k) for p,k in VAL_PATTERNS]
PUEUE_RE = re.compile(r"\bpueue\b", re.I)
SLEEP_RE = re.compile(r"\bsleep\s+\d|while\s+true|until\s+", re.I)

def val_kind(cmd):
    if not cmd: return None
    # ignore obvious meta-greps over transcripts
    for rx,k in VAL_RE:
        if rx.search(cmd): return k
    return None

FAIL_RE = re.compile(r"(\bFAIL\b|✖|✗|×\s|\bfailed\b|\bfailing\b|Error:|ERR_|error TS\d|exit code [1-9]|Exit code: [1-9]|not ok \d|Tests?:\s+\d+ failed|\d+ failed|✘|AssertionError|Command failed|npm ERR!|ELIFECYCLE)", re.I)
PASS_RE = re.compile(r"(\ball (checks|tests) pass|✓|✔|PASS\b|passed\b|\bok\b\s|0 failed|Exit code: 0|Done in )", re.I)

def main():
    # load events grouped by file (session)
    sessions = collections.defaultdict(list)
    seen=set()
    for line in open("events.jsonl"):
        o=json.loads(line)
        if o["uuid"] in seen: continue
        seen.add(o["uuid"])
        sessions[o["f"]].append(o)
    print("sessions", len(sessions))
    # index results by tool_use_id per session
    allcalls=[]
    out=open("valcalls.jsonl","w")
    for f, evs in sessions.items():
        res={}
        for e in evs:
            if e["k"]=="r":
                for r in e["res"]:
                    res[r["id"]]=(e,r)
        for idx,e in enumerate(evs):
            if e["k"]!="a": continue
            for t in e["tools"]:
                if t["name"]!="Bash": continue
                cmd=t["cmd"] or ""
                k=val_kind(cmd)
                pu = bool(PUEUE_RE.search(cmd))
                if not k and not pu: continue
                ent=res.get(t["id"])
                body=""; err=None
                if ent:
                    re_,r_=ent
                    body=r_["body"]
                    err=r_["err"]
                    ex=re_.get("extra") or {}
                    if not body:
                        body=(ex.get("stdout") or "")+"\n"+(ex.get("stderr") or "")
                rec={"f":f,"ln":e["ln"],"idx":idx,"uuid":e["uuid"],"ts":e["ts"],"sid":e["sid"],
                     "side":e["side"],"kind":k or "pueue","pueue":pu,"cmd":cmd[:1200],
                     "err":err,"body":body[:4000],"has_res":ent is not None}
                out.write(json.dumps(rec)+"\n")
                allcalls.append(rec)
    out.close()
    print("val bash calls", len(allcalls))
    print("by kind", collections.Counter(c["kind"] for c in allcalls).most_common())
    print("err flag", collections.Counter(str(c["err"]) for c in allcalls))
main()
