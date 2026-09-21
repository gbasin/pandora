#!/usr/bin/env python3
"""Build per-session step timelines and classify post-red turns."""
import json, os, re, collections

OUT = os.path.dirname(os.path.abspath(__file__))
S = json.load(open(os.path.join(OUT, "sessions.json")))

TRIAL_RE = re.compile(r"_int-(pandora-[a-z0-9-]+?-\d{8})/")

VALID_CMD = re.compile(
    r"(pnpm\s+(run\s+)?(test:surface|journey|journeys|validate)\b"
    r"|pnpm\s+(run\s+)?build\b"
    r"|docker\s+build\b|docker\s+run\b|docker\s+image\s+rm\b)")
WAITCMD = re.compile(r"pandora\s+wait\b")
POLLCMD = re.compile(r"\b(sleep|pueue\s+status|ps\s+-|tail\s+-f)\b")
EDIT_TOOL = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
READ_TOOL = {"Read", "Grep", "Glob", "LS", "read_file"}


def trial(s):
    cwd = s.get("cwd") or ""
    m = TRIAL_RE.search(cwd)
    return m.group(1) if m else (cwd.split("/")[-1] or "unknown")


def lane(s):
    return (s.get("cwd") or "").rstrip("/").split("/")[-1]


CMD_JSON = re.compile(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"')
CMD_BARE = re.compile(r'\bcmd\s*:\s*"((?:[^"\\]|\\.)*)"')


def codex_tool(ev):
    inp = ev.get("input") or ""
    if not isinstance(inp, str):
        inp = json.dumps(inp)
    if "apply_patch" in inp:
        return "apply_patch", inp[:400]
    if "write_stdin" in inp:
        return "write_stdin", inp[:300]
    if "read_file" in inp:
        return "read_file", inp[:300]
    m = CMD_JSON.search(inp) or CMD_BARE.search(inp)
    if m:
        return "exec", m.group(1).encode().decode("unicode_escape", "replace")
    return "exec", inp[:300]


def steps(s):
    """Return ordered list of step dicts with tool, cmd, result text."""
    res_by_id = {}
    for e in s["events"]:
        if e["kind"] == "tool_result":
            res_by_id[e.get("id")] = e
    out = []
    for e in s["events"]:
        if e["kind"] != "tool_use":
            continue
        if s["agent"] == "codex":
            tool, cmd = codex_tool(e)
        else:
            tool = e.get("tool")
            inp = e.get("input") or {}
            cmd = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
            if tool in EDIT_TOOL:
                cmd = inp.get("file_path", "")
        r = res_by_id.get(e.get("id"))
        out.append(dict(line=e["line"], tool=tool, cmd=cmd,
                        result=(r or {}).get("text", ""), is_error=(r or {}).get("is_error"),
                        usage=e.get("usage") or {}))
    return out


def step_kind(st):
    tool, cmd = st["tool"], st["cmd"] or ""
    if tool in EDIT_TOOL:
        return "edit"
    if tool == "write_stdin":
        return "poll_stdin"
    if tool in READ_TOOL:
        return "read"
    if WAITCMD.search(cmd):
        return "wait_cmd"
    if POLLCMD.search(cmd):
        return "poll_cmd"
    if VALID_CMD.search(cmd):
        return "validate"
    if re.search(r"\bsed\s+-i\b|\bcat\s*>\s|\btee\b|python3?\s+-c.*write", cmd):
        return "edit"
    return "other"


RED = re.compile(r"(\d+\s+failed|FAIL\b|Error:|✘|✕|exit(?:ed)? (?:code )?[1-9]|EXIT=[1-9]|"
                 r"failed\b|Test failed|assertion|\"ok\"\s*:\s*false)", re.I)
GREEN = re.compile(r"(\d+\s+passed|All tests passed|exit code 0|EXIT=0|\"ok\"\s*:\s*true|PASS\b)", re.I)
INFRA = re.compile(r"(exit(?:ed)? (?:code )?7[05]\b|active request|already (?:active|running)|"
                   r"stale result|not supported|unsupported|rejected|Pandora .*(refus|block)|"
                   r"queue|waiting for|admitted|infrastructure failure|pandora wait)", re.I)


def outcome(st):
    t = st.get("result") or ""
    # prefer explicit exit code
    m = re.search(r'"exit_code"\s*:\s*(-?\d+)', t)
    ex = int(m.group(1)) if m else None
    g = bool(GREEN.search(t))
    r = bool(RED.search(t))
    return dict(exit=ex, green=g, red=r)


rows = []
for s in S:
    st = steps(s)
    for x in st:
        x["kind"] = step_kind(x)
        if x["kind"] in ("validate", "wait_cmd"):
            x["out"] = outcome(x)
    rows.append(dict(agent=s["agent"], trial=trial(s), lane=lane(s), path=s["path"],
                     steps=st))
json.dump(rows, open(os.path.join(OUT, "steps.json"), "w"))

c = collections.Counter()
for r in rows:
    for x in r["steps"]:
        c[(r["agent"], x["kind"])] += 1
for k, v in sorted(c.items()):
    print(k, v)
print("total steps", sum(c.values()))
