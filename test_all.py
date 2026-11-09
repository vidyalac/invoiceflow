"""Runs every sample in the intended order against a fresh database and checks the decisions."""
import os, sys, json, shutil
os.environ["STATE_DIR"] = "/tmp/if_test_state"
shutil.rmtree("/tmp/if_test_state", ignore_errors=True)
import store, pipeline
store.init()
base = os.path.dirname(os.path.abspath(__file__))
manifest = json.load(open(os.path.join(base, "samples", "manifest.json")))
order = ["01", "02", "03", "04a", "04b", "04c", "05", "06", "07", "08", "09"]
bad = 0
for pre in order:
    m = next(x for x in manifest if x["file"].startswith(pre + "_"))
    rid = pipeline.new_run(m["file"], os.path.join(base, "samples", m["file"]))
    pipeline.execute(rid, delay=0)
    r = pipeline.RUNS[rid]
    dec = r["result"]["decision"]
    ok = dec == m["expected"]
    bad += not ok
    print(("OK  " if ok else "FAIL"), m["file"], "->", dec, "(expected", m["expected"] + ")", f"{r['duration_ms']}ms")
    if "-v" in sys.argv or not ok:
        for f in r["result"]["findings"]:
            print("      ", f["sev"], f["code"], "-", f["msg"])
        if not ok:
            print("       fields:", {k: v["value"] for k, v in r["view"].get("fields", {}).items()})
print("stats:", store.stats()["by"])
sys.exit(1 if bad else 0)
