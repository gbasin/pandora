#!/usr/bin/env python3
"""v2 classifier: tighter rules, post-red segments, queue-time extraction."""
import json, os, re, collections

OUT = os.path.dirname(os.path.abspath(__file__))
S = json.load(open(os.path.join(OUT, "sessions.json")))

TRIAL_RE = re.compile(r"_int-(pandora-[a-z0-9-]+?-\d{8})/")
EDIT_TOOL = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
READ_TOOL = {"Read", "Grep", "Glob", "LS", "read_file", "ToolSearch"}

VALID_CMD = re.compile(
    r"(pnpm\s+(run\s+)?(test:surface|journey|journeys)\b"
    r"|pnpm\s+(run\s+)?validate\s+(surface|journey|journeys)\b"
    r"|pnpm\s+(run\s+)?build\b"
    r"|docker\s+build\b|docker\s+run\b)")
WAIT_CMD = re.compile(r"(pandora\s+wait\b|^\s*sleep\s+\d|;\s*sleep\s+\d|\bsleep\s+\d+\s*;|"
                      r"tail\s+-f\b|\buntil\s+|\bwhile\s+.*kill\s+-0)")
INFRA_HUNT = re.compile(r"(pueue\b|\bps\s+(aux|-axo|-ef)\b|docker\s+(ps|version|images|inspect)\b"
                        r"|find\s+\S*\.local/state/pandora|rg\s+-l\b.*\.local/state/pandora"
                        r"|ls\s+.*\.local/state/pandora|systemctl|journalctl|\bssh\b"
                        r"|active\.json|command -v pnpm|which pnpm|type pnpm)")
PROVENANCE = re.compile(r"\bgit\s+(log|show|diff|status|blame|stash|rev-parse)\b")
SHELLEDIT = re.compile(r"(\bsed\s+-i\b|python3?\s+-\s*<<|python3?\s+-\s+<<|\bcat\s*>\s*[^|>]|\btee\s+)")

REJECT = re.compile(
    r"(Validation is already active|already active for this worktree|"
    r'"exit_code"\s*:\s*7[05]\b|exit code 7[05]\b|'
    r"stale result|submitted nothing|"
    r"is not a supported|not supported|unsupported|Unknown cleanup|"
    r"CreateProcess|Rejected\()", re.I)
VERDICT_RED = re.compile(r'("exit_code"\s*:\s*[1-9]\d*|EXIT=[1-9]|\b\d+\s+failed\b|'
                         r'\bTest failed\b|AssertionError|✘|"status"\s*:\s*"fail")', re.I)
VERDICT_GREEN = re.compile(r'("exit_code"\s*:\s*0\b|EXIT=0|\b\d+\s+passed\b|"status"\s*:\s*"pass")', re.I)
QUEUE_S = re.compile(r"invocation queue ([\d.]+)s/")
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
        return "write_stdin", "write_stdin(poll)"
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
    if "total_tokens" in u:
        return u["total_tokens"]
    return (u.get("input_tokens", 0) + u.get("output_tokens", 0) +
            u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))


def build(s):
    res = {e.get("id"): e for e in s["events"] if e["kind"] == "tool_result"}
    per_line = collections.defaultdict(list)
    for e in s["events"]:
        if e["kind"] == "tool_use":
            per_line[e.get("msgid") or e["line"]].append(e)
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
        steps.append(dict(line=e["line"], tool=tool, cmd=cmd or "",
                          result=r.get("text", "") or "",
                          tokens=billed(e.get("usage")) /
                          max(1, len(per_line[e.get("msgid") or e["line"]]))))
    return steps


def classify(steps):
    seen = []                 # (cmd, edits_at_time)
    edits = 0
    bg = set()
    for st in steps:
        cmd, tool, result = st["cmd"], st["tool"], st["result"]
        m = re.search(r"Output is being written to: (\S+)", result)
        if m:
            bg.add(m.group(1))
        is_valid = bool(VALID_CMD.search(cmd))
        st["queue_s"] = max([float(x) for x in QUEUE_S.findall(result)] or [0])

        if tool in EDIT_TOOL or (tool in ("Bash", "exec") and SHELLEDIT.search(cmd) and not is_valid):
            edits += 1
            st["cat"] = "edit"
            continue
        if tool == "write_stdin" or tool in ("Monitor", "TaskStop", "BashOutput"):
            st["cat"] = "wait_poll"
            continue
        if tool in READ_TOOL and (cmd in bg or "file exists but the contents are empty" in result):
            st["cat"] = "wait_poll"
            continue
        if WAIT_CMD.search(cmd):
            st["cat"] = "wait_poll"
            continue
        if is_valid:
            dup = any(c == cmd and e == edits for c, e in seen)
            seen.append((cmd, edits))
            rejected = bool(REJECT.search(result)) and not VERDICT_GREEN.search(result)
            if rejected:
                st["cat"] = "redundant_validate" if dup else "infra_confusion"
            elif dup and not VERDICT_RED.search(result) and not VERDICT_GREEN.search(result):
                st["cat"] = "redundant_validate"
            elif dup:
                st["cat"] = "redundant_validate"
            else:
                st["cat"] = "validate"
            continue
        if INFRA_HUNT.search(cmd):
            st["cat"] = "infra_confusion"
            continue
        if REJECT.search(result):
            st["cat"] = "infra_confusion"
            continue
        if PROVENANCE.search(cmd):
            st["cat"] = "provenance"
            continue
        if tool in READ_TOOL or re.search(r"\b(cat|sed -n|rg|grep|head|tail|ls|jq|find|wc|cmp|diff)\b",
                                          cmd.lower()):
            st["cat"] = "diagnose"
            continue
        st["cat"] = "other"
    return steps


rows = []
for s in S:
    st = classify(build(s))
    rows.append(dict(agent=s["agent"], trial=trial(s), lane=lane(s), path=s["path"], steps=st))
json.dump(rows, open(os.path.join(OUT, "classified2.json"), "w"))

CATS = ["validate", "edit", "diagnose", "provenance", "wait_poll",
        "redundant_validate", "infra_confusion", "other"]
agg = collections.defaultdict(collections.Counter)
tok = collections.defaultdict(collections.Counter)
for r in rows:
    for x in r["steps"]:
        agg[r["agent"]][x["cat"]] += 1
        agg["ALL"][x["cat"]] += 1
        tok[r["agent"]][x["cat"]] += x["tokens"]
        tok["ALL"][x["cat"]] += x["tokens"]

na = sum(agg["ALL"].values())
tt = sum(tok["ALL"].values())
print(f"{'category':20s} {'claude':>7s} {'codex':>7s} {'ALL':>7s} {'%turns':>7s} {'tokM':>8s} {'%tok':>6s}")
for c in CATS:
    print(f"{c:20s} {agg['claude'][c]:7d} {agg['codex'][c]:7d} {agg['ALL'][c]:7d} "
          f"{100*agg['ALL'][c]/na:6.1f}% {tok['ALL'][c]/1e6:8.2f} {100*tok['ALL'][c]/tt:5.1f}%")
print(f"{'TOTAL':20s} {sum(agg['claude'].values()):7d} {sum(agg['codex'].values()):7d} {na:7d}"
      f"          {tt/1e6:8.2f}")
print()
# queue seconds observed
qs = [x["queue_s"] for r in rows for x in r["steps"] if x["queue_s"] > 0]
print(f"steps reporting queue wait: {len(qs)}, max observed {max(qs) if qs else 0:.1f}s")
