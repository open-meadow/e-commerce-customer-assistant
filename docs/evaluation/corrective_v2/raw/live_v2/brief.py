import json,sys,io
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")
for f in sys.argv[1:]:
    d=json.load(open(f,encoding="utf-8")); print("=====",f)
    for r in d["review_sheet"]:
        names=[c["name"]+("("+",".join(str(v) for v in (c.get("arguments") or {}).values())+")" if c.get("arguments") else "") for c in r["tool_calls"]]
        print(f'{r["id"]} t{r["turn"]} tools={names} flags={r["automated_flags"]}')
        print("   >",r["reply"].replace("\n"," / ")[:330])
