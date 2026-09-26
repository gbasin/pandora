import re

HEREDOC = re.compile(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?\s*\n.*?\n\1\s*(?=\n|$)", re.S)

def strip_noise(cmd):
    c = HEREDOC.sub(" <<HEREDOC> ", cmd)
    # remove single/double quoted long strings (PR bodies, grep patterns)
    c = re.sub(r"'(?:[^']{40,})'", " 'STR' ", c)
    c = re.sub(r'"(?:[^"]{40,})"', ' "STR" ', c)
    return c

SPLIT = re.compile(r"(?:\|\||&&|\||;|\n|\bthen\b|\bdo\b|&)")
LEAD = re.compile(r"^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)|(?:timeout\s+\S+\s+)|(?:nice\s+(?:-n\s*\S+\s+)?)|(?:env\s+)|(?:time\s+)|(?:caffeinate\s+(?:-\S+\s+)*)|(?:script\s+-q\s+\S+\s+)|(?:stdbuf\s+\S+\s+))+")

PNPM_VAL = re.compile(r"^(?:pnpm|npm|yarn)\b(?P<rest>.*)$")
VALSCRIPTS = {
 "validate":"validate","check":"check","test":"test","typecheck":"typecheck","lint":"lint",
 "build":"build","journey":"journey","journeys":"journey","format":"format","e2e":"playwright",
}
def classify_segment(seg):
    s = LEAD.sub("", seg.strip())
    s = re.sub(r"^\(\s*","",s)
    if not s: return None
    tok = s.split()
    if not tok: return None
    t0 = tok[0].split("/")[-1]
    if t0 in ("pnpm","npm","yarn"):
        rest = tok[1:]
        # skip flags/filters
        i=0
        while i < len(rest):
            r = rest[i]
            if r in ("run","--filter","-F","--dir","-C","-w","--workspace-root","exec","-r","--recursive","--silent","-s"):
                if r in ("--filter","-F","--dir","-C"): i+=2
                else: i+=1
                continue
            if r.startswith("-"): i+=1; continue
            break
        if i>=len(rest): return None
        sub = rest[i]
        base = sub.split(":")[0]
        if base in VALSCRIPTS:
            kind = VALSCRIPTS[base]
            if base=="check" and sub.startswith("check:"): kind="check"
            return (kind, " ".join(rest[i:i+4]))
        if sub in ("vitest",): return ("vitest"," ".join(rest[i:i+4]))
        if sub in ("playwright",): return ("playwright"," ".join(rest[i:i+4]))
        if sub in ("oxlint",): return ("lint","oxlint")
        if sub in ("oxfmt",): return ("format","oxfmt")
        if sub in ("tsc",): return ("typecheck","tsc")
        if sub in ("turbo",): return ("turbo"," ".join(rest[i:i+4]))
        return None
    if t0=="npx":
        rest=tok[1:]
        if not rest: return None
        n=rest[0]
        if n=="playwright": return ("playwright"," ".join(rest[:3]))
        if n=="vitest": return ("vitest"," ".join(rest[:3]))
        if n=="tsc": return ("typecheck","tsc")
        return None
    if t0=="node" and "--test" in tok[:3]: return ("nodetest"," ".join(tok[:4]))
    if t0=="vitest": return ("vitest"," ".join(tok[:3]))
    if t0=="playwright": return ("playwright"," ".join(tok[:3]))
    if t0=="oxlint": return ("lint","oxlint")
    if t0=="oxfmt": return ("format","oxfmt")
    if t0=="tsc": return ("typecheck","tsc")
    if t0=="turbo": return ("turbo"," ".join(tok[:3]))
    if t0=="pueue": return ("pueue"," ".join(tok[:3]))
    if t0 in ("sleep",): return ("sleep"," ".join(tok[:2]))
    return None

def analyze(cmd):
    c=strip_noise(cmd)
    hits=[]
    for seg in SPLIT.split(c):
        r=classify_segment(seg)
        if r: hits.append(r)
    return hits
