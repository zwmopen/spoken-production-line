"""Read and validate production plans without navigating or accepting commentary."""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from start_ready import atomic_save_queue


def conversation_snapshot(page, conversation_id):
    """Read one conversation through the authenticated page context (read only)."""
    js = """(async()=>{
      const s=await fetch('/api/auth/session').then(r=>r.json());
      const r=await fetch('/backend-api/conversation/'+CID,{headers:{Authorization:'Bearer '+s.accessToken}});
      if(!r.ok)return JSON.stringify({ok:false,status:r.status,retryAfter:r.headers.get('Retry-After')});
      const d=await r.json();let n=d.current_node;const chain=[];
      while(n&&d.mapping[n]){const x=d.mapping[n];if(x.message){const m=x.message;chain.push({
        id:m.id,role:m.author?.role,status:m.status,
        text:(m.content?.parts||[]).filter(x=>typeof x==='string').join(' '),
        attachments:(m.metadata?.attachments||[]).map(x=>x.name||x.filename||'')
      })}n=x.parent}
      return JSON.stringify({ok:true,id:d.conversation_id,title:d.title,messages:chain.reverse()})
    })()""".replace("CID", json.dumps(conversation_id))
    return json.loads(page.evaluate(js, timeout=45))


def page_numbers(text):
    found = {int(n) for n in re.findall(r"(?<![A-Za-z0-9])P\s*(\d{1,2})(?!\d)", text, re.I)}
    found.update(int(n) for n in re.findall(r"第\s*(\d{1,2})\s*页", text))
    return sorted(found)


def is_plan(text):
    text = (text or "").strip()
    pages = page_numbers(text)
    structured = any(k in text for k in ("逐页", "页码", "页面规划", "页数依据", "迁移计划"))
    return len(text) >= 700 and len(pages) >= 2 and structured and pages[0] == 1 and pages == list(range(1, pages[-1] + 1))


def target_user_message(messages, row):
    prompt = " ".join((row.get("prompt") or "").split())
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if message.get("role") != "user" or " ".join((message.get("text") or "").split()) != prompt:
            continue
        expected = row.get("serverAttachments") or []
        actual = message.get("attachments") or []
        if len(actual) != len(row.get("files", [])):
            return None, "ATTACHMENT_COUNT_MISMATCH"
        if expected and sorted(actual) != sorted(expected):
            return None, "ATTACHMENT_NAME_MISMATCH"
        end = next((j for j in range(i + 1, len(messages)) if messages[j].get("role") == "user"), len(messages))
        return (i, end), "MATCHED"
    return None, "EXACT_SUBMISSION_NOT_FOUND"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument("--adapter")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.adapter:
        sys.path.insert(0, args.adapter)
    import cdp_adapter as core

    path = Path(args.queue)
    queue = json.loads(path.read_text(encoding="utf-8-sig"))
    port = queue.get("cdpPort", 9431 if queue["account"] == "A" else 9432)
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
    target = next((t for t in targets if t.get("type") == "page" and t.get("url", "").startswith("https://chatgpt.com/")), None)
    if not target:
        raise SystemExit("No authenticated ChatGPT page available; no queue changes made")
    page = core.CDP(target["webSocketDebuggerUrl"])
    try:
        results = []
        for row in queue["tasks"]:
            if row.get("status") not in {"SUBMITTED", "PLAN_READY"} or row.get("generationStatus"):
                continue
            conversation_id = row.get("conversationId") or row.get("conversationUrl", "").rsplit("/c/", 1)[-1]
            result = {"id": row["id"], "checkedAt": time.time()}
            try:
                snap = conversation_snapshot(page, conversation_id)
                if not snap.get("ok") or snap.get("id") != conversation_id:
                    result.update(status="CONVERSATION_READ_FAILED", httpStatus=snap.get("status"))
                    row.update(planReadbackStatus=result["status"], planReadbackCheckedAt=result["checkedAt"])
                    results.append(result)
                    atomic_save_queue(path, queue)
                    continue
                messages = snap.get("messages", [])
                segment, binding = target_user_message(messages, row)
                if not segment:
                    result.update(status=binding)
                    row.update(planReadbackStatus=binding, planReadbackCheckedAt=result["checkedAt"])
                else:
                    start, end = segment
                    replies = [m for m in messages[start + 1:end] if m.get("role") == "assistant"]
                    valid = [m for m in replies if m.get("status") == "finished_successfully" and is_plan(m.get("text", ""))]
                    pending = any(m.get("status") == "in_progress" for m in replies)
                    if valid:
                        plan = valid[-1]
                        nums = page_numbers(plan.get("text", ""))
                        row.update(status="PLAN_READY", plan=plan["text"], planMessageId=plan["id"],
                                   planReadAt=result["checkedAt"], plannedPageCount=len(nums), planPageNumbers=nums,
                                   planReadbackStatus="VALID_PLAN", planReadbackCheckedAt=result["checkedAt"])
                        result.update(status="PLAN_READY", planMessageId=plan["id"], pageNumbers=nums, planChars=len(plan["text"]))
                    elif pending:
                        row.update(planReadbackStatus="GENERATING", planReadbackCheckedAt=result["checkedAt"])
                        result.update(status="GENERATING")
                    else:
                        row.update(planReadbackStatus="NO_VALID_PLAN", planReadbackCheckedAt=result["checkedAt"])
                        result.update(status="NO_VALID_PLAN", assistantReplies=len(replies),
                                      validPlanChars=[len(m.get("text", "")) for m in replies])
                results.append(result)
            except Exception as exc:
                row.update(planReadbackStatus="READ_ERROR", planReadbackCheckedAt=result["checkedAt"],
                           planReadbackError=type(exc).__name__ + ": " + str(exc)[:240])
                result.update(status="READ_ERROR", error=type(exc).__name__ + ": " + str(exc)[:240])
                results.append(result)
            atomic_save_queue(path, queue)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        page.close()


if __name__ == "__main__":
    main()
