#!/usr/bin/env python3
import json, os, sys, re
OUT = os.path.dirname(os.path.abspath(__file__))
rows = json.load(open(os.path.join(OUT, "steps.json")))
pat = sys.argv[1]
n = int(sys.argv[2]) if len(sys.argv) > 2 else 400
for r in rows:
    if pat not in r["lane"]:
        continue
    print("=" * 100)
    print(r["agent"], r["lane"])
    print(r["path"])
    for x in r["steps"]:
        cmd = (x["cmd"] or "").replace("\n", " ; ")[:160]
        res = (x["result"] or "").replace("\n", " ; ")[:n]
        print(f"L{x['line']:>5} [{x['kind']:10s}] {x['tool']:12s} {cmd}")
        print(f"        -> {res}")
