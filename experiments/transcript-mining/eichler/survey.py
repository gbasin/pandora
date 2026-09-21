import json, re, collections
seen=set(); cmds=collections.Counter(); nbash=0; ntool=collections.Counter()
for line in open("events.jsonl"):
    o=json.loads(line)
    if o["uuid"] in seen: continue
    seen.add(o["uuid"])
    if o["k"]!="a": continue
    for t in o["tools"]:
        ntool[t["name"]]+=1
        if t["name"]=="Bash" and t["cmd"]:
            c=t["cmd"]
            # first meaningful token sequence
            head=re.sub(r"\s+"," ",c.strip())[:60]
            cmds[head]+=1
            nbash+=1
print("bash calls",nbash)
print(ntool.most_common(20))
for k,v in cmds.most_common(60): print(v,"|",k)
