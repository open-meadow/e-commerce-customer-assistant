import json,sys,io
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")
f=sys.argv[1]; truth=len(sys.argv)>2 and sys.argv[2]=="truth"
d=json.load(open(f,encoding="utf-8"))
for r in d["review_sheet"]:
    tools=[(c["name"],c.get("arguments")) for c in r["tool_calls"]]
    res=[(x["name"],x.get("result") or x.get("status")) for x in r["tool_results"]]
    print(f'## {r["id"]} t{r["turn"]} [{r["customer_id"]}] "{r["customer_message"]}"')
    print("  tools:",tools,"| results:",res,"| added:",r["engine_added_reads"],"| recov:",r["item_recoveries"],"| flags:",r["automated_flags"])
    print("  REPLY:",r["reply"].replace("\n"," / "))
    if truth: print("  TRUTH:",r["ground_truth"])
