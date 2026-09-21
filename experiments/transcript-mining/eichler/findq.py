import json,sys,os
def find(f, needle):
    for i,l in enumerate(open(f,errors="replace"),1):
        if needle in l:
            try: o=json.loads(l)
            except: continue
            return i, o.get("timestamp")
    return None,None
Q=[
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/e5510ac7-4fed-44d1-ba61-94983dd2249a.jsonl","S3-27 is a pre-existing failure"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/e5510ac7-4fed-44d1-ba61-94983dd2249a.jsonl","It failed one step earlier on the base branch"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/e5510ac7-4fed-44d1-ba61-94983dd2249a.jsonl","either timing flakiness or an interaction I can"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler--claude-worktrees-journey/86e4f7aa-2cc0-4428-8920-660974ac7616.jsonl","Two consecutive failures"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/b39c7fd6-be24-42d0-b091-cfa860a51cc2.jsonl","known `act` flake"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/b39c7fd6-be24-42d0-b091-cfa860a51cc2.jsonl","Three consecutive clean runs"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/97d50b44-cb49-43ec-9d76-56c1c6e2ccc0.jsonl","burned real time rediscovering"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/97d50b44-cb49-43ec-9d76-56c1c6e2ccc0.jsonl","Stale Maestro driver processes"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/edf17eb4-1ffe-430e-adc6-c8d09fb315ce.jsonl","still serving the mutation-3 build"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/ac0ad83f-342c-40a6-9e6b-6e599b284995.jsonl","Loopback ports are exhausted machine-wide"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/17e50d3f-1dd1-4c6e-b478-fd1bc7fe2032.jsonl","is still running and has been making commits"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/9a2ee25a-a292-49cd-a2d4-e3adfcf6387d.jsonl","Main moved the lockfile when I rebased"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler--claude-worktrees-blueprint-replay/835bb971-e3fb-4295-a3b9-2da41cf03d12.jsonl","Fresh worktree needs its install"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler--claude-worktrees-licensing-filing-plan/e53d402d-9f78-45d1-97c8-37598f79f6fd.jsonl","the format failure is just missing node_modules"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/bfe420f5-562a-4f7c-9eed-f00d393705b4.jsonl","cannot call them unrelated to my change"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/e5510ac7-4fed-44d1-ba61-94983dd2249a.jsonl","Wrangler hadn't hot-reloaded"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/08208d99-d3b6-4330-82da-f5a2de8ccbfb.jsonl","a real regression, not pre-existing"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/4d18e0a8-dbea-47f8-87d7-60dc0f17cca4.jsonl","unrelated to the change and looks like a flake"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/4d18e0a8-dbea-47f8-87d7-60dc0f17cca4.jsonl","I will not assume a flake"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/bb44086d-5b45-4dfd-844f-98974cab5c67.jsonl","encrypted with another stack's keys"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/1614321e-fb9d-42ea-9d29-b4a1d19ed671.jsonl","It is a race in the test itself"),
("/Users/garybasin/.claude/projects/-Users-garybasin-Code-eichler/bfe420f5-562a-4f7c-9eed-f00d393705b4.jsonl","The paging is now breaking validation itself"),
]
for f,q in Q:
    i,ts=find(f,q)
    print(f"{i}\t{ts}\t{os.path.basename(f)}\t{q[:60]}")
