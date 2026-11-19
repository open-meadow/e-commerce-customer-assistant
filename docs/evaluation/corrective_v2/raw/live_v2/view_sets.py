import json,sys,io
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")
d=json.load(open(sys.argv[1],encoding="utf-8"))
print("integrity:",d["integrity"]["run_valid_for_scoring"],d["integrity"]["provider_error_replies"])
for n,s in d["sets"].items():
    m=s["summary"]; print(n,"conversations",m["conversations_passed"],"/",m["conversations_total"],"keys",m["turn_expectations_passed"],"/",m["turn_expectations"],"universal",m["universal_checks_passed"],"/",m["universal_checks_applicable"],"| failed",[(x["conversation"],x["failed"]) for x in m["failed_turns"]])
print("used from:",{k:v["used"] for k,v in d["composition"]["per_test"]["conversations"].items()})
for r in d["review_sheet"]:
    names=[c["name"]+"("+",".join(str(v) for v in (c.get("arguments") or {}).values())+")" for c in r["tool_calls"]]
    print(f'## {r["id"]} [{r["customer_id"]}] "{r["customer_message"]}"')
    print("  tools:",names,"| results:",[(x["name"],x.get("result") or x.get("status")) for x in r["tool_results"]],"| added:",r["engine_added_reads"],"| recov:",r["item_recoveries"],"| flags:",r["automated_flags"])
    print("  REPLY:",r["reply"].replace("\n"," / "))
    print("  TRUTH:",r["ground_truth"])
