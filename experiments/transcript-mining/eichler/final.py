import json, collections, os, statistics
d=os.path.dirname(os.path.abspath(__file__))
eps=[json.loads(l) for l in open(d+"/episodes_c.jsonl")]
for i,e in enumerate(eps): e["id"]=i

MANUAL={
1:"g",3:"g",28:"g",76:"a",81:"a",99:"g",101:"g",213:"g",243:"g",247:"g",306:"g",331:"g",
399:"b",105:"g",106:"g",155:"g",172:"g",322:"f",10:"x",9:"g",55:"g",323:"f",364:"e",377:"e",
66:"f",254:"g",250:"g",370:"g",365:"g",72:"g",25:"g",380:"g",196:"g",136:"g",392:"b",88:"g",246:"f",
96:"g",94:"g",319:"g",57:"f",337:"g",50:"f",358:"x",
292:"f",14:"f",6:"f",374:"f",262:"g",244:"g",89:"g",51:"g",385:"g",
80:"a",151:"g",160:"g",342:"g",418:"g",20:"g",393:"g",141:"f",383:"g",280:"g",152:"g",300:"f",411:"f",
}
A2M={"a_flaky":"a","b_preexisting":"b","c_rerun":"c","d_bisect":"d","e_wait":"e","f_env":"f","g_real":"g"}
conf=collections.defaultdict(collections.Counter)
for e in eps:
    if e["id"] in MANUAL: conf[e["auto"]][MANUAL[e["id"]]]+=1
tot=0; agree=0
print("CONFUSION (auto -> manual)")
for a in sorted(conf):
    n=sum(conf[a].values()); ok=conf[a][A2M[a]]
    tot+=n; agree+=ok
    print(f"  {a:14s} n={n:2d} precision={ok}/{n}={ok/n:.2f}  {dict(conf[a])}")
print(f"  OVERALL agreement {agree}/{tot} = {agree/tot:.2f}  -> observed classifier error rate {1-agree/tot:.2f}")

pop=collections.Counter(e["auto"] for e in eps)
print("\nPOPULATION (auto) ", dict(pop), "total", len(eps))
# corrected counts
est=collections.Counter()
for a,n in pop.items():
    c=conf[a]; s=sum(c.values())
    for m,k in c.items(): est[m]+= n*k/s
print("CORRECTED ESTIMATE of category share (episodes):")
for m in sorted(est, key=lambda x:-est[x]):
    print(f"   {m}: {est[m]:.0f}  ({est[m]/len(eps)*100:.0f}%)")

# cost: mean turns / out per auto stratum (population) -> redistribute
print("\nPOPULATION COST per auto stratum")
for a in sorted(pop):
    s=[e for e in eps if e["auto"]==a]
    print(f"  {a:14s} n={len(s):3d} turns={sum(e['turns'] for e in s):5d} out={sum(e['out'] for e in s):8d} cr={sum(e['cr'] for e in s):12d}")
estT=collections.Counter(); estO=collections.Counter(); estC=collections.Counter()
for a in pop:
    s=[e for e in eps if e["auto"]==a]
    T=sum(e['turns'] for e in s); O=sum(e['out'] for e in s); C=sum(e['cr'] for e in s)
    c=conf[a]; tt=sum(c.values())
    for m,k in c.items():
        estT[m]+=T*k/tt; estO[m]+=O*k/tt; estC[m]+=C*k/tt
print("\nCORRECTED COST ESTIMATE (upper-bound window)")
for m in sorted(estT, key=lambda x:-estT[x]):
    print(f"   {m}: turns {estT[m]:.0f}  out {estO[m]:.0f}  cacheread {estC[m]:.0f}")

# tight cost: green-closed only
g=[e for e in eps if e["closed_by"]=="green"]
print("\nTIGHT (green-closed) n=",len(g),"turns",sum(e['turns'] for e in g),"out",sum(e['out'] for e in g),"cr",sum(e['cr'] for e in g))
gm=[e for e in g if e["id"] in MANUAL]
print("   of which manually reviewed:",len(gm))

# top 12
eps.sort(key=lambda e:-e["out"])
print("\nTOP 12 EPISODES BY OUTPUT TOKENS")
for e in eps[:12]:
    lab=MANUAL.get(e["id"],"?")
    print(f'  EP{e["id"]:<4d} auto={e["auto"]:13s} manual={lab} kind={e["kind"]:9s} turns={e["turns"]:3d} out={e["out"]:6d} closed={e["closed_by"]:6s}')
    print(f'        {e["f"]}:{e["start_ln"]}')
