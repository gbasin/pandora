#!/usr/bin/env python3
"""Read-only extraction of agent session timelines from Claude Code and Codex JSONL."""
import json, os, re, glob, sys

HOME = os.path.expanduser("~")
OUT = os.path.dirname(os.path.abspath(__file__))


def jload(line):
    try:
        return json.loads(line)
    except Exception:
        return None


# ---------------- Claude ----------------
def claude_sessions():
    base = os.path.join(HOME, ".claude", "projects")
    for d in sorted(os.listdir(base)):
        if "pandora-" not in d:
            continue
        for f in sorted(glob.glob(os.path.join(base, d, "*.jsonl"))):
            yield d, f


def parse_claude(path, projdir):
    events = []
    cost = None
    cwd = None
    prompt = None
    lineno = 0
    for line in open(path, errors="replace"):
        lineno += 1
        d = jload(line)
        if not d:
            continue
        t = d.get("type")
        if t == "cost-state":
            cost = d
        elif t == "user" and d.get("promptSource") == "sdk" and prompt is None:
            c = d["message"].get("content")
            prompt = c if isinstance(c, str) else json.dumps(c)
            cwd = d.get("cwd")
        elif t == "assistant":
            m = d["message"]
            if cwd is None:
                cwd = d.get("cwd")
            u = m.get("usage") or {}
            mid = m.get("id") or d.get("requestId") or f"L{lineno}"
            for b in m.get("content", []):
                bt = b.get("type")
                if bt == "text":
                    events.append(dict(line=lineno, ts=d.get("timestamp"), kind="text",
                                       text=b.get("text", ""), usage=u, msgid=mid))
                elif bt == "thinking":
                    events.append(dict(line=lineno, ts=d.get("timestamp"), kind="thinking",
                                       text=b.get("thinking", ""), usage=u, msgid=mid))
                elif bt == "tool_use":
                    inp = b.get("input", {})
                    events.append(dict(line=lineno, ts=d.get("timestamp"), kind="tool_use",
                                       tool=b.get("name"), id=b.get("id"),
                                       cmd=inp.get("command") or inp.get("file_path") or
                                           inp.get("pattern") or inp.get("path") or "",
                                       input=inp, usage=u, msgid=mid))
        elif t == "user":
            c = d["message"].get("content")
            if isinstance(c, list):
                for b in c:
                    if b.get("type") == "tool_result":
                        con = b.get("content")
                        if isinstance(con, list):
                            con = "".join(x.get("text", "") for x in con if isinstance(x, dict))
                        events.append(dict(line=lineno, ts=d.get("timestamp"), kind="tool_result",
                                           id=b.get("tool_use_id"), is_error=b.get("is_error"),
                                           text=con if isinstance(con, str) else json.dumps(con)))
    return dict(agent="claude", path=path, project=projdir, cwd=cwd, prompt=prompt,
                events=events, cost=cost)


# ---------------- Codex ----------------
def codex_sessions():
    base = os.path.join(HOME, ".codex", "sessions", "2026", "09")
    for day in ("19", "20"):
        for f in sorted(glob.glob(os.path.join(base, day, "*.jsonl"))):
            yield f


CMD_RE = re.compile(r'cmd\s*:\s*(".*?"|\'.*?\'|`.*?`)', re.S)


def codex_cmd(inp):
    if not isinstance(inp, str):
        return ""
    m = CMD_RE.search(inp)
    if m:
        return m.group(1)[1:-1]
    return inp[:300]


def parse_codex(path):
    events = []
    cwd = None
    prompt = None
    total_usage = None
    lineno = 0
    last_usage = {}
    for line in open(path, errors="replace"):
        lineno += 1
        d = jload(line)
        if not d:
            continue
        t = d.get("type")
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if t == "session_meta":
            cwd = p.get("cwd")
            if cwd is None:
                cwd = d.get("cwd")
        elif t == "token_usage_record":
            total_usage = p.get("thread_token_usage") or total_usage
            last_usage = p.get("usage") or {}
            # usage record follows the response it belongs to: back-fill
            for e in reversed(events):
                if e.get("usage") is None:
                    e["usage"] = last_usage
                else:
                    break
        elif t == "response_item":
            pt = p.get("type")
            if pt == "custom_tool_call":
                events.append(dict(line=lineno, kind="tool_use", tool=p.get("name"),
                                   id=p.get("call_id"), cmd=codex_cmd(p.get("input")),
                                   input=p.get("input"), usage=None,
                                   ts=(p.get("internal_chat_message_metadata_passthrough") or {}).get("create_time")))
            elif pt == "custom_tool_call_output":
                out = p.get("output")
                if isinstance(out, list):
                    out = "".join(x.get("text", "") for x in out if isinstance(x, dict))
                events.append(dict(line=lineno, kind="tool_result", id=p.get("call_id"),
                                   text=out if isinstance(out, str) else json.dumps(out),
                                   ts=(p.get("internal_chat_message_metadata_passthrough") or {}).get("create_time")))
            elif pt == "function_call":
                events.append(dict(line=lineno, kind="tool_use", tool=p.get("name"),
                                   id=p.get("call_id"), cmd=codex_cmd(p.get("arguments")),
                                   input=p.get("arguments"), usage=None, ts=None))
            elif pt == "function_call_output":
                o = p.get("output")
                events.append(dict(line=lineno, kind="tool_result", id=p.get("call_id"),
                                   text=o if isinstance(o, str) else json.dumps(o), ts=None))
            elif pt == "message":
                role = p.get("role")
                txt = "".join(c.get("text", "") for c in p.get("content", []) if isinstance(c, dict))
                if role == "user" and prompt is None and "Pandora" in txt:
                    prompt = txt
                elif role == "assistant":
                    events.append(dict(line=lineno, kind="text", text=txt, usage=None, ts=None))
        elif t == "event_msg" and p.get("type") == "item_completed":
            it = p.get("item", {})
            if it.get("type") == "UserMessage" and prompt is None:
                prompt = "".join(c.get("text", "") for c in it.get("content", []) if isinstance(c, dict))
        elif t == "event_msg" and p.get("type") == "task_complete":
            events.append(dict(line=lineno, kind="final", text=p.get("last_agent_message", ""),
                               duration_ms=p.get("duration_ms")))
    return dict(agent="codex", path=path, project=None, cwd=cwd, prompt=prompt,
                events=events, usage=total_usage)


def main():
    out = []
    for projdir, f in claude_sessions():
        s = parse_claude(f, projdir)
        if s["events"]:
            out.append(s)
    for f in codex_sessions():
        # cheap cwd prefilter
        head = open(f, errors="replace").readline()
        d = jload(head) or {}
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        cwd = p.get("cwd") or d.get("cwd") or ""
        if "pandora" not in cwd:
            continue
        s = parse_codex(f)
        if s["events"]:
            out.append(s)
    with open(os.path.join(OUT, "sessions.json"), "w") as fh:
        json.dump(out, fh)
    print("sessions:", len(out))
    print("claude:", sum(1 for s in out if s["agent"] == "claude"))
    print("codex:", sum(1 for s in out if s["agent"] == "codex"))


if __name__ == "__main__":
    main()
