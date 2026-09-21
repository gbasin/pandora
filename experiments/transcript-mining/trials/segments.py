#!/usr/bin/env python3
"""Post-red segment analysis + blocking wall-clock measurement."""
import json, os, re, collections, statistics as st
from datetime import datetime

OUT = os.path.dirname(os.path.abspath(__file__))
R = json.load(open(os.path.join(OUT, "classified2.json")))
S = {s["path"]: s for s in json.load(open(os.path.join(OUT, "sessions.json")))}

VERDICT_RED = re.compile(r'("exit_code"\s*:\s*[1-9]\d*|EXIT=[1-9]|\b\d+\s+failed\b|'
                         r'\bTest failed\b|AssertionError|✘|"status"\s*:\s*"fail")', re.I)
VERDICT_GREEN = re.compile(r'("exit_code"\s*:\s*0\b|EXIT=0|\b\d+\s+passed\b|"status"\s*:\s*"pass")', re.I)
REJECT = re.compile(r"(Validation is already active|\"exit_code\"\s*:\s*7[05]\b|submitted nothing"
                    r"|CreateProcess|Rejected\(|not supported|unsupported)", re.I)

# --- 1. red verdicts and what the agent did next
segs = []
for r in R:
    steps = r["steps"]
    for i, x in enumerate(steps):
        txt = x["result"]
        red = (x["cat"] in ("validate", "wait_poll", "redundant_validate")
               and VERDICT_RED.search(txt) and not REJECT.search(txt)
               and not VERDICT_GREEN.search(txt))
        if not red:
            continue
        # walk forward to next edit
        cats = []
        for y in steps[i + 1:]:
            if y["cat"] == "edit":
                break
            cats.append(y["cat"])
        else:
            cats.append("<no edit followed>")
        segs.append(dict(agent=r["agent"], lane=r["lane"], line=x["line"],
                         n=len(cats), cats=collections.Counter(cats)))

print(f"red verdicts detected: {len(segs)} "
      f"(claude {sum(1 for s in segs if s['agent']=='claude')}, "
      f"codex {sum(1 for s in segs if s['agent']=='codex')})")
tot = collections.Counter()
for s in segs:
    tot.update(s["cats"])
print("turns between a red verdict and the next edit, by category:")
N = sum(tot.values())
for k, v in tot.most_common():
    print(f"  {k:22s} {v:5d} {100*v/max(1,N):5.1f}%")
lens = [s["n"] for s in segs]
if lens:
    print(f"  segment length: median {st.median(lens)}, mean {st.mean(lens):.1f}, max {max(lens)}")

# --- 2. blocking wall clock inside single tool calls (Claude only: has timestamps)
print()
durs = []
for path, s in S.items():
    if s["agent"] != "claude":
        continue
    tu = {}
    for e in s["events"]:
        if e["kind"] == "tool_use":
            tu[e["id"]] = e
        elif e["kind"] == "tool_result" and e.get("id") in tu:
            a, b = tu[e["id"]].get("ts"), e.get("ts")
            if not a or not b:
                continue
            d = (datetime.fromisoformat(b.replace("Z", "+00:00")) -
                 datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()
            inp = tu[e["id"]].get("input") or {}
            cmd = inp.get("command") or ""
            if re.search(r"pnpm\s+(run\s+)?(test:surface|journey|journeys|build)|docker\s+(build|run)", cmd):
                durs.append((d, s["path"], cmd[:60], (e.get("text") or "")[:60]))
durs.sort(reverse=True)
print(f"claude foreground validation calls timed: {len(durs)}")
if durs:
    v = [d for d, *_ in durs]
    print(f"  wall seconds: median {st.median(v):.1f}, mean {st.mean(v):.1f}, max {max(v):.1f}, "
          f"total {sum(v)/60:.1f} min")
    print("  longest 8:")
    for d, p, c, res in durs[:8]:
        print(f"    {d:7.1f}s  {c}   [{os.path.basename(os.path.dirname(p))[-40:]}]")

# --- 3. duplicate/rejected submissions per session
print()
rej = [(r["agent"], r["lane"], x["line"], x["cmd"][:60])
       for r in R for x in r["steps"]
       if x["cat"] in ("redundant_validate",) or
       (x["cat"] == "infra_confusion" and REJECT.search(x["result"]))]
print(f"rejected/duplicate validation submissions: {len(rej)}")
c = collections.Counter(a for a, *_ in rej)
print(" ", dict(c))
for a, l, ln, cm in rej[:25]:
    print(f"  [{a}] {l[-42:]:42s} L{ln:<5} {cm}")
