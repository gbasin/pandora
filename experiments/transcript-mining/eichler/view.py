import json, sys, os, textwrap
d=os.path.dirname(os.path.abspath(__file__))
eps=[json.loads(l) for l in open(d+"/episodes_c.jsonl")]
idx={ (e["f"],e["si"]):e for e in eps}
def show(e, ntext=6000):
    print("#"*100)
    print("FILE", e["f"], "line", e["start_ln"], "kind", e["kind"], "auto", e["auto"], "turns", e["turns"], "out", e["out"], "closed_by", e["closed_by"], "nreds", e["nreds"], "dup", e["dup_runs"])
    print("-- CMD --"); print(" ".join(e["cmd"].split())[:700])
    print("-- FAIL BODY (tail) --"); print("\n".join(e["body"].splitlines()[-25:])[:2000])
    print("-- fails", e["fails"], "envs", e["envs"], "exit", e["exit"])
    print("-- SUBSEQUENT CMDS --")
    for c in e["cmds"][1:25]: print("   $", " ".join(c.split())[:200])
    print("-- TEXT --")
    print(e["text"][:ntext])
if __name__=="__main__":
    key=sys.argv[1]
    sel=[e for e in eps if key in e["f"] and (len(sys.argv)<3 or str(e["si"])==sys.argv[2])]
    for e in sel[:int(sys.argv[3]) if len(sys.argv)>3 else 3]: show(e)
