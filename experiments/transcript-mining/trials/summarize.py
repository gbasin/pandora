#!/usr/bin/env python3
import json, os, re, collections

OUT = os.path.dirname(os.path.abspath(__file__))
S = json.load(open(os.path.join(OUT, "sessions.json")))

TRIAL_RE = re.compile(r"_int-(pandora-[a-z0-9-]+?-\d{8})/")
LANE_RE = re.compile(r"\.worktrees/([^/]+)$")


def trial(s):
    cwd = s.get("cwd") or ""
    m = TRIAL_RE.search(cwd)
    if m:
        return m.group(1)
    return cwd.split("/")[-1] or "unknown"


def lane(s):
    cwd = s.get("cwd") or ""
    return cwd.rstrip("/").split("/")[-1]


VALID = re.compile(r"(pnpm\s+(run\s+)?(test:surface|journey|journeys|validate)\b|docker\s+(build|run|image\s+rm)\b|pandora\s+wait\b)")


def tokens(s):
    if s["agent"] == "claude":
        c = s.get("cost") or {}
        mu = c.get("modelUsage") or {}
        tot = dict(input=0, output=0, cache_read=0, cache_create=0, cost=c.get("totalCostUSD"))
        for k, v in mu.items():
            tot["input"] += v.get("inputTokens", 0)
            tot["output"] += v.get("outputTokens", 0)
            tot["cache_read"] += v.get("cacheReadInputTokens", 0)
            tot["cache_create"] += v.get("cacheCreationInputTokens", 0)
        return tot
    u = s.get("usage") or {}
    return dict(input=u.get("input_tokens", 0), output=u.get("output_tokens", 0),
                cache_read=u.get("cached_input_tokens", 0), cache_create=u.get("cache_write_input_tokens", 0),
                cost=None)


rows = []
for s in S:
    ev = s["events"]
    tu = [e for e in ev if e["kind"] == "tool_use"]
    val = [e for e in tu if VALID.search(e.get("cmd") or "")]
    rows.append(dict(agent=s["agent"], trial=trial(s), lane=lane(s), path=s["path"],
                     n_events=len(ev), n_tool=len(tu), n_text=sum(1 for e in ev if e["kind"] == "text"),
                     n_valid=len(val), tokens=tokens(s)))

by = collections.Counter((r["trial"], r["agent"]) for r in rows)
print(f"{'trial':55s} {'agent':7s} n  tools valid")
agg = collections.defaultdict(lambda: [0, 0, 0, 0])
for r in sorted(rows, key=lambda r: (r["trial"], r["agent"], r["lane"])):
    print(f"{r['trial'][:55]:55s} {r['agent']:7s} {r['n_tool']:4d} {r['n_valid']:3d}  {r['lane'][:50]}")
print()
for (t, a), n in sorted(by.items()):
    print(f"{t[:55]:55s} {a:7s} {n}")
json.dump(rows, open(os.path.join(OUT, "rows.json"), "w"), indent=1)
