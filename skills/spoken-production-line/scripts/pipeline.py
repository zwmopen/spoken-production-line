"""One local entry for spoken intake, production, and durable inventory.
The JSON queues/manifests remain authoritative; SQLite is a rebuildable query view.
"""
import argparse,datetime as dt,json,pathlib,sqlite3,subprocess,sys
from production_supervisor import account_lock,save

CLOSED={"ARCHIVED","COMPLETED","COMPLETE","COMPLETED_BY_REPLAN","ARCHIVED_ACCEPTED","ACCEPTED_REPAIRED_NOTIFIED","ACCEPTED_ARCHIVED_AND_NOTIFIED"}
HELD={"USER_REJECTED_REPLAN_REQUIRED","REPAIR_NOTIFIED","QUARANTINED","REJECTED_BY_USER_FEEDBACK"}
def read(p): return json.loads(p.read_text(encoding="utf-8-sig"))
def phase(row):
    s=str(row.get("status","")).upper()
    if s in CLOSED:return "complete"
    if s in HELD:return "held_for_review"
    if row.get("copySubmissionIntent") or s.startswith("COPY"):return "copy"
    if row.get("startMessageId") or row.get("generationSubmissionIntent") or row.get("submissionIntent"):return "reconcile_generation"
    if s=="PLAN_READY":return "plan_ready"
    if s in {"SUBMITTED","SUBMIT_CLICKED_NOT_VERIFIED","SUBMISSION_UNKNOWN_NOT_VERIFIED"}:return "reconcile_plan"
    if s in {"PREPARED_NOT_UPLOADED","WAITING_UPLOAD_BUDGET"}:return "intake_ready"
    return "needs_recovery"

def inventory(root):
    rows=[]
    for a in ("A","B"):
        q=read(root/("queue-"+a+".json"))
        for x in q["tasks"]:
            rows.append({"workId":x.get("workId") or x["id"],"account":a,"phase":phase(x),"sourcePath":x.get("sourcePath") or x.get("sourceFolderPath"),"templateId":x.get("templateId"),"planMessageId":x.get("planMessageId"),"startMessageId":x.get("startMessageId"),"manifestPath":x.get("manifestPath"),"queueStatus":x.get("status"),"lastProgressAt":x.get("lastProgressAt") or x.get("planReadbackFinishedAt")})
    with sqlite3.connect(root/"产线查询.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS works (account TEXT,work_id TEXT,phase TEXT,payload TEXT,PRIMARY KEY(account,work_id))")
        db.execute("DELETE FROM works")
        db.executemany("INSERT INTO works VALUES(?,?,?,?)",[(x["account"],x["workId"],x["phase"],json.dumps(x,ensure_ascii=False)) for x in rows])
    save(root/"产线阶段索引.json",{"derivedAt":dt.datetime.now(dt.timezone.utc).isoformat(),"source":"queue-A/B + referenced manifests; query cache only","rows":rows})
    return rows

def dispatch(root,account,mode,execute=False):
    rows=[r for r in inventory(root) if r["account"]==account and r["phase"] not in {"complete","held_for_review"}]
    active=[r for r in rows if r["phase"] in {"copy","reconcile_generation","reconcile_plan","needs_recovery"}]
    decision={"account":account,"mode":mode,"checkedAt":dt.datetime.now(dt.timezone.utc).isoformat(),"executeRequested":execute}
    if active:
        decision.update(action="resume_existing",workIds=[x["workId"] for x in active],reason="Prior in-flight or unknown tasks must be reconciled; never blanket-clear queues")
    elif mode=="intake" or (mode=="auto" and not any(r["phase"]=="plan_ready" for r in rows)):
        ready=[r for r in rows if r["phase"]=="intake_ready"]
        decision.update(action="upload_prepared" if ready else "discover_material",workIds=[r["workId"] for r in ready],reason="Upload allowance is attachments including TXT, not a fixed eight suites")
        if execute and ready:
            with account_lock(root/"supervisor"/(account+".lock")):
                result=subprocess.run([sys.executable,"-X","utf8",str(pathlib.Path(__file__).with_name("upload_queue.py")),"--queue",str(root/("queue-"+account+".json"))],capture_output=True,text=True,encoding="utf-8",timeout=1200)
            decision.update(returncode=result.returncode,output=result.stdout[-3000:],error=result.stderr[-1000:])
    else:
        decision.update(action="submit_reviewed_batch",workIds=[r["workId"] for r in rows if r["phase"]=="plan_ready"])
        if execute:
            from batch_launch_queue import tick
            decision["receipt"]=tick(root,account)
    decision["fullyAutomated"]=False
    decision["remainingAdapters"]=["new-source discovery and queue insertion","asset recovery and semantic page mapping","bounded visual acceptance","copy/usage/archive/notification closure"]
    save(root/("调度决定-"+account+".json"),decision)
    return decision

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("command",choices=["status","intake","produce","auto"]);p.add_argument("--task-root",type=pathlib.Path,required=True);p.add_argument("--account",choices=["A","B"]);p.add_argument("--execute",action="store_true");a=p.parse_args();root=a.task_root;(root/"supervisor").mkdir(exist_ok=True)
    if a.command=="status":print(json.dumps(inventory(root),ensure_ascii=False))
    else:
        if not a.account:p.error("--account is required for dispatch")
        print(json.dumps(dispatch(root,a.account,a.command,a.execute),ensure_ascii=False))
if __name__=="__main__":main()
