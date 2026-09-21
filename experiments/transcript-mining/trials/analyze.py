#!/usr/bin/env python3
"""Classify every agent tool call in the Pandora trials and attribute tokens."""
import json, os, re, collections

OUT = os.path.dirname(os.path.abspath(__file__))
S = json.load(open(os.path.join(OUT, "sessions.json")))

TRIAL_RE = re.compile(r"_int-(pandora-[a-z0-9-]+?-\d{8})/")
EDIT_TOOL = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
READ_TOOL = {"Read", "Grep", "Glob", "LS", "read_file", "ToolSearch"}

VALID_CMD = re.compile(
    r"(pnpm\s+(run\s+)?(test:surface|journey|journeys|validate)\b"
    r"|pnpm\s+(run\s+)?build\b"
    r"|docker\s+build\b|docker\s+run\b|docker\s+image\s+rm\b)")
WAIT_CMD = re.compile(r"(pandora\s+wait\b|\bsleep\s+\d|tail\s+-f\b|\buntil\s|\bwhile\s+.*kill\s+-0)")
INFRA_PROBE = re.compile(r"(pueue\b|\bps\s+(aux|-axo|-ef)|docker\s+(ps|version|images|inspect)\b"
                         r"|\.local/state/pandora|systemctl|journalctl|ssh\b)")
PROVENANCE = re.compile(r"\bgit\s+(log|show|diff|status|blame|stash|rev-parse)\b")
SHELLEDIT = re.compile(r"(\bsed\s+-i\b|python3?\s+-\s*<<|apply_patch|\bcat\s*>\s*[^|]|\btee\s+)")

# Pandora routing / infrastructure feedback in a tool RESULT
INFRA_RESULT = re.compile(
    r"(Validation is already active|already active for this worktree|"
    r'"exit_code"\s*:\s*7[05]\b|exit 7[05]\b|'
    r"stale result|changed input was not submitted|submitted nothing|"
    r"not a supported|unsupported|is not routed|Pandora does not|"
    r"recover feedback with|pandora wait|queued;|slots occupied|"
    r"infrastructure failure|CreateProcess|Rejected\()", re.I)
QUEUE_RESULT = re.compile(r"(queued;|slots occupied|invocation queue \d|waiting for the experiment|"
                          r"freezing current tracked|transferring changed source|"
                          r"input verification complete|worker acquired)", re.I)
RED_RESULT = re.compile(r'("exit_code"\s*:\s*[1-9]|EXIT=[1-9]|\d+\s+failed|Test failed|'
                        r'\bexpected\b.*\breceived\b|AssertionError|✘|"ok"\s*:\s*false)', re.I)
GREEN_RESULT = re.compile(r'("exit_code"\s*:\s*0|EXIT=0|\d+\s+passed|all \d+ tests passed|"ok"\s*:\s*true)', re.I)

CMD_JSON = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')
CMD_BARE = re.compile(r'\bcmd\s*:\s*"((?:[^"\\]|\\.)*)"')


def trial(s):
    cwd = s.get("cwd") or ""
    m = TRIAL_RE.search(cwd)
    return m.group(1) if m else (cwd.split("/")[-1] or "unknown")


def lane(s):
    return (s.get("cwd") or "").rstrip("/").split("/")[-1]


def codex_tool(ev):
    inp = ev.get("input") or ""
    if not isinstance(inp, str):
        inp = json.dumps(inp)
    if "apply_patch" in inp:
        return "apply_patch", "apply_patch"
    if "write_stdin" in inp:
        return "write_stdin", inp[:200]
    if "read_file" in inp:
        return "read_file", inp[:200]
    m = CMD_JSON.search(inp) or CMD_BARE.search(inp)
    if m:
        try:
            return "exec", m.group(1).encode().decode("unicode_escape")
        except Exception:
            return "exec", m.group(1)
    return "exec", inp[:200]


def billed(u):
    if not u:
        return 0
    if "total_tokens" in u:            # codex
        return u["total_tokens"]
    return (u.get("input_tokens", 0) + u.get("output_tokens", 0) +
            u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))


def session_total_tokens(s):
    if s["agent"] == "claude":
        c = s.get("cost") or {}
        tot = 0
        for v in (c.get("modelUsage") or {}).values():
            tot += (v.get("inputTokens", 0) + v.get("outputTokens", 0) +
                    v.get("cacheReadInputTokens", 0) + v.get("cacheCreationInputTokens", 0))
        return tot, c.get("totalCostUSD")
    tot = 0
    for e in s["events"]:
        if e.get("usage"):
            tot += billed(e["usage"])
    return tot, None


def build(s):
    res = {e.get("id"): e for e in s["events"] if e["kind"] == "tool_result"}
    # tokens per assistant message line, split over its tool calls
    per_line = collections.defaultdict(list)
    for e in s["events"]:
        if e["kind"] == "tool_use":
            per_line[e["line"]].append(e)
    steps = []
    for e in s["events"]:
        if e["kind"] != "tool_use":
            continue
        if s["agent"] == "codex":
            tool, cmd = codex_tool(e)
        else:
            tool = e.get("tool") or ""
            inp = e.get("input") or {}
            cmd = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
        r = res.get(e.get("id")) or {}
        tok = billed(e.get("usage")) / max(1, len(per_line[e["line"]]))
        steps.append(dict(line=e["line"], tool=tool, cmd=cmd or "",
                          result=r.get("text", "") or "", tokens=tok))
    return steps


def classify(agent, steps):
    """Assign one category per step, using running state."""
    seen_validations = []      # (cmd, edits_at_time)
    edits = 0
    bg_files = set()
    for st in steps:
        cmd, tool, result = st["cmd"], st["tool"], st["result"]
        low = cmd.lower()
        is_valid = bool(VALID_CMD.search(cmd))
        # remember background output paths handed back by the harness
        m = re.search(r"Output is being written to: (\S+)", result)
        if m:
            bg_files.add(m.group(1))

        # ---- edits
        if tool in EDIT_TOOL or (tool in ("Bash", "exec") and SHELLEDIT.search(cmd)
                                 and not is_valid):
            edits += 1
            st["cat"] = "edit"
            continue
        # ---- polling / waiting
        if tool == "write_stdin":
            st["cat"] = "wait_poll"
            continue
        if tool in ("Monitor", "TaskStop", "BashOutput"):
            st["cat"] = "wait_poll"
            continue
        if tool in READ_TOOL and cmd in bg_files:
            st["cat"] = "wait_poll"
            continue
        if tool in READ_TOOL and ("Warning: the file exists but the contents are empty" in result
                                  or "file exists but the contents are empty" in result):
            st["cat"] = "wait_poll"
            continue
        if WAIT_CMD.search(cmd) and not is_valid:
            st["cat"] = "wait_poll"
            continue
        if WAIT_CMD.search(cmd) and is_valid:
            st["cat"] = "wait_poll"       # e.g. "sleep 30; pnpm journey"
            continue
        # ---- validation
        if is_valid:
            dup = any(c == cmd and e == edits for c, e in seen_validations)
            seen_validations.append((cmd, edits))
            if INFRA_RESULT.search(result) and not RED_RESULT.search(result[:4000]):
                st["cat"] = "redundant_validate" if dup else "infra_confusion"
            elif dup:
                st["cat"] = "redundant_validate"
            else:
                st["cat"] = "validate"
            continue
        # ---- infrastructure probing / confusion
        if INFRA_PROBE.search(cmd):
            st["cat"] = "infra_confusion"
            continue
        if INFRA_RESULT.search(result) and not GREEN_RESULT.search(result):
            st["cat"] = "infra_confusion"
            continue
        # ---- provenance
        if PROVENANCE.search(cmd):
            st["cat"] = "provenance"
            continue
        # ---- diagnosis / inspection
        if tool in READ_TOOL or re.search(r"\b(cat|sed -n|rg|grep|head|tail|ls|jq|find|wc)\b", low):
            st["cat"] = "diagnose"
            continue
        st["cat"] = "other"
    return steps


rows = []
for s in S:
    st = classify(s["agent"], build(s))
    tot, cost = session_total_tokens(s)
    rows.append(dict(agent=s["agent"], trial=trial(s), lane=lane(s), path=s["path"],
                     steps=st, total_tokens=tot, cost=cost))
json.dump(rows, open(os.path.join(OUT, "classified.json"), "w"))

CATS = ["validate", "edit", "diagnose", "provenance", "wait_poll",
        "redundant_validate", "infra_confusion", "other"]
agg = collections.defaultdict(lambda: collections.Counter())
tokagg = collections.defaultdict(lambda: collections.Counter())
for r in rows:
    for x in r["steps"]:
        agg[r["agent"]][x["cat"]] += 1
        tokagg[r["agent"]][x["cat"]] += x["tokens"]
        agg["ALL"][x["cat"]] += 1
        tokagg["ALL"][x["cat"]] += x["tokens"]

print(f"{'category':20s} {'claude':>8s} {'codex':>8s} {'ALL':>8s} {'%all':>6s} {'tok(ALL,M)':>11s} {'%tok':>6s}")
tt = sum(tokagg["ALL"].values()) or 1
na = sum(agg["ALL"].values()) or 1
for c in CATS:
    print(f"{c:20s} {agg['claude'][c]:8d} {agg['codex'][c]:8d} {agg['ALL'][c]:8d} "
          f"{100*agg['ALL'][c]/na:5.1f}% {tokagg['ALL'][c]/1e6:11.2f} {100*tokagg['ALL'][c]/tt:5.1f}%")
print(f"{'TOTAL':20s} {sum(agg['claude'].values()):8d} {sum(agg['codex'].values()):8d} {na:8d}        {tt/1e6:11.2f}")
print()
print("session total tokens (from cost-state / usage records):")
ct = sum(r["total_tokens"] for r in rows if r["agent"] == "claude")
xt = sum(r["total_tokens"] for r in rows if r["agent"] == "codex")
print(f"  claude {ct/1e6:.2f}M over {sum(1 for r in rows if r['agent']=='claude')} sessions, "
      f"cost ${sum(r['cost'] or 0 for r in rows):.2f}")
print(f"  codex  {xt/1e6:.2f}M over {sum(1 for r in rows if r['agent']=='codex')} sessions")
