"""Start exactly one verified page with write-ahead intent and read-only reconciliation."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from production_supervisor import account_lock


class SubmissionOutcomeUnknown(RuntimeError):
    """The send may have reached the server; the next attempt must reconcile only."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def build_page_prompt(template: str, page: int) -> str:
    """Replace the single page token. A literal P# is never sent to the model."""
    if type(page) is not int or page < 1:
        raise ValueError("page must be a positive integer")
    if template.count("P#") != 1:
        raise ValueError("production prompt must contain exactly one P# token")
    return template.replace("P#", f"P{page}")


def prepare_submission_intent(task: dict, page: int, prompt: str,
                              now: str | None = None,
                              intent_id: str | None = None) -> dict:
    """Persist an idempotency record before the browser send can occur."""
    if task.get("generationStatus") or task.get("startMessageId") or task.get("submissionIntent"):
        raise ValueError("task already has a submission state; reconcile before retry")
    plan_id = task.get("planMessageId")
    if not plan_id:
        raise ValueError("missing verified planMessageId")
    if intent_id is not None and (not isinstance(intent_id, str) or
                                  not intent_id or len(intent_id) > 160):
        raise ValueError("intent_id must be a non-empty string of at most 160 characters")
    intent = {
        "id": intent_id or uuid.uuid4().hex,
        "workId": task.get("workId") or task.get("id"),
        "page": page,
        "planMessageId": plan_id,
        "createdAt": now or utc_now(),
        "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "status": "SUBMISSION_UNKNOWN",
        "reconcileOnly": True,
    }
    task.update(
        activePage=page,
        generationStatus="SUBMISSION_UNKNOWN",
        submissionAttemptedAt=int(time.time()),
        submissionIntent=intent,
        awaitingGenerationCompletion=False,
    )
    return intent


def is_unstarted_plan(row: dict) -> bool:
    return (row.get("status") == "PLAN_READY"
            and not row.get("generationStatus")
            and not row.get("submissionIntent")
            and not row.get("planReviewIssues"))


def select_ready_task(queue: dict, work_id: str | None = None,
                      slots: int = 1) -> dict | None:
    """Select only an eligible plan; an exact requested workId never falls through."""
    if slots <= 0:
        return None
    tasks = queue.get("tasks", [])
    if work_id:
        matches = [row for row in tasks
                   if (row.get("workId") or row.get("id")) == work_id]
        if len(matches) != 1:
            raise LookupError("requested workId is missing or ambiguous in the exact queue")
        row = matches[0]
        if not is_unstarted_plan(row):
            raise ValueError("requested workId is not a verified unstarted plan; reconcile before retry")
        return row
    return next((row for row in tasks if is_unstarted_plan(row)), None)


def atomic_save_queue(path: Path, queue: dict) -> None:
    """Replace the queue atomically so a crash cannot truncate task state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    queue["updatedAt"] = utc_now()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(queue, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _active_generation(page) -> bool:
    return bool(page.evaluate(
        "[...document.querySelectorAll('button')].some(b=>"
        "/^(停止|Stop|Stop generating)$/.test(b.getAttribute('aria-label')||'')"
        "&&b.getBoundingClientRect().width>0)"
    ))


def _safe_page_target(core, ui, port: int, conversation_url: str):
    """Read every ChatGPT tab first; refuse navigation if any page is active/drafted."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.load(response)
    pages = [target for target in targets
             if target.get("type") == "page"
             and target.get("url", "").startswith("https://chatgpt.com/")]
    if not pages:
        raise RuntimeError("no existing authenticated ChatGPT page; refusing to create or switch profile")

    exact = None
    first = pages[0]
    for target in pages:
        probe = core.CDP(target["webSocketDebuggerUrl"], timeout=8)
        try:
            state = ui.visible_state(probe)
            if _active_generation(probe):
                raise RuntimeError("a ChatGPT tab is generating; preserve every page and do not navigate")
            if state.get("removers") or any(editor.get("text", "").strip()
                                             for editor in state.get("editors", [])):
                raise RuntimeError("a ChatGPT tab has a draft/attachments; preserve every page and do not navigate")
            if target.get("url", "").split("?", 1)[0] == conversation_url.split("?", 1)[0]:
                exact = target
        finally:
            probe.close()
    target = exact or first
    return target, target.get("url", "").split("?", 1)[0] == conversation_url.split("?", 1)[0]


def _insert_prompt(page, prompt: str, ui) -> None:
    """Use the renderer's editor command; avoid CDP Input.insertText focus hangs."""
    chunks = [prompt[i:i + 1000] for i in range(0, len(prompt), 1000)]
    for chunk in chunks:
        value = json.dumps(chunk, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        result = page.evaluate(
            "(()=>{const c=document.querySelector('#prompt-textarea')||"
            "document.querySelector('div[contenteditable=true][role=textbox]')||"
            "document.querySelector('[role=textbox][contenteditable=true]')||"
            "(()=>{const es=[...document.querySelectorAll('[contenteditable=true]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0});return es.length===1?es[0]:null})();"
            f"if(!c)return -1;c.focus();const ok=document.execCommand('insertText',false,{value});"
            "return ok?((c.innerText!==undefined?c.innerText:c.value)||'').length:-2})()"
        )
        if not isinstance(result, int) or result < 0:
            raise RuntimeError("editor rejected execCommand insertion")
    state = ui.visible_state(page)
    if not any(" ".join(editor["text"].split()) == " ".join(prompt.split())
               for editor in state.get("editors", [])):
        raise RuntimeError("page did not confirm the exact prompt in its composer")
    send_ready = page.evaluate(
        "(()=>{const b=document.querySelector('button[data-testid=send-button]')||"
        "[...document.querySelectorAll('button')].find(x=>"
        "/^(发送|发送提示词|Send prompt)$/.test(x.getAttribute('aria-label')||''));"
        "return !!b&&b.getBoundingClientRect().width>0&&!b.disabled})()"
    )
    if not send_ready:
        raise RuntimeError("send button did not become enabled after composer verification")


def _conversation_chain(page, conversation_id: str):
    js = """(async()=>{const s=await fetch('/api/auth/session').then(r=>r.json());
    const r=await fetch('/backend-api/conversation/'+CID,{headers:{Authorization:'Bearer '+s.accessToken}});
    if(!r.ok)throw Error('Conversation read failed '+r.status);const d=await r.json();
    let n=d.current_node;const out=[];while(n&&d.mapping[n]){const x=d.mapping[n],m=x.message;
    if(m)out.push({id:m.id,role:m.author?.role,text:(m.content?.parts||[])
      .filter(x=>typeof x==='string').join(' ')});n=x.parent}return JSON.stringify(out.reverse())})()""".replace(
        "CID", json.dumps(conversation_id)
    )
    return json.loads(page.evaluate(js, timeout=45))


def _run_locked(args) -> int:
    path = Path(args.queue)
    queue = json.loads(path.read_text(encoding="utf-8-sig"))
    account = queue["account"]
    port = queue.get("cdpPort", 9431 if account == "A" else 9432)
    protection = path.parent / f"{account}运行保护.json"
    if protection.exists() and json.loads(protection.read_text(encoding="utf-8-sig")).get("protectActiveGeneration"):
        print("Active generation protected; no browser operation", flush=True)
        return 0

    slots = max(0, min(args.count, 1))
    # A caller may still pass --work-id while setting --count 0. Treat the
    # zero-slot request as a no-op; never let an exact id bypass the slot cap.
    row = select_ready_task(queue, args.work_id, slots)
    if not row:
        print("No verified unstarted plan slots; no command sent", flush=True)
        return 0
    if not row.get("planMessageId") or not row.get("conversationUrl"):
        raise RuntimeError("missing exact plan/conversation identity")
    page_number = args.page or row.get("activePage") or row.get("nextPage") or 1
    prompt_path = Path(__file__).resolve().parent.parent / "references" / "production-confirm-prompt.txt"
    prompt_template = prompt_path.read_text(encoding="utf-8-sig").strip()
    prompt = build_page_prompt(prompt_template, page_number)
    conversation_id = row["conversationUrl"].split("/c/")[-1].split("?", 1)[0]

    if args.adapter:
        sys.path.insert(0, args.adapter)
        import sync_batch as core
        import sync_batch_direct_ui as ui
    else:
        import cdp_adapter as core
        import cdp_adapter as ui

    target, already_on_target = _safe_page_target(core, ui, port, row["conversationUrl"])
    page = core.CDP(target["webSocketDebuggerUrl"])
    try:
        if not already_on_target:
            page.call("Page.navigate", {"url": row["conversationUrl"]})
        deadline = time.monotonic() + 90
        state = ui.visible_state(page)
        while time.monotonic() < deadline:
            state = ui.visible_state(page)
            if state.get("ready") == "complete" and state.get("editors"):
                break
            time.sleep(2)
        else:
            raise TimeoutError("target conversation did not become ready")

        if state.get("removers") or any(editor.get("text", "").strip()
                                         for editor in state.get("editors", [])):
            raise RuntimeError("target conversation has a draft/attachments; no production triggered")
        if _active_generation(page):
            raise RuntimeError("target conversation is generating; do not send again")

        before = _conversation_chain(page, conversation_id)
        texts = [message for message in before if message.get("text", "").strip()]
        if not texts or texts[-1]["id"] != row["planMessageId"]:
            raise RuntimeError("latest message is no longer the verified plan")

        _insert_prompt(page, prompt, ui)

        # Write-ahead record: once this is durable, every retry is read-only reconciliation.
        intent = prepare_submission_intent(row, page_number, prompt,
                                           intent_id=args.intent_id)
        atomic_save_queue(path, queue)

        page.evaluate(
            "(()=>{const b=document.querySelector('button[data-testid=send-button]')||"
            "[...document.querySelectorAll('button')].find(x=>"
            "/^(发送|发送提示词|Send prompt)$/.test(x.getAttribute('aria-label')||''));"
            "if(!b||b.disabled)throw Error('send unavailable');b.click();return true})()"
        )
        intent["clickIssuedAt"] = utc_now()
        atomic_save_queue(path, queue)

        deadline = time.monotonic() + 180
        before_ids = {message["id"] for message in before}
        while time.monotonic() < deadline:
            after = _conversation_chain(page, conversation_id)
            sent = [message for message in after
                    if message["id"] not in before_ids
                    and message.get("role") == "user"
                    and " ".join(message.get("text", "").split()) == " ".join(prompt.split())]
            if sent:
                row.update(
                    generationStatus="START_SUBMITTED",
                    startMessageId=sent[-1]["id"],
                    generationStartedAt=int(time.time()),
                    awaitingGenerationCompletion=True,
                )
                intent.update(status="SUBMITTED_CONFIRMED", confirmedAt=utc_now(),
                              startMessageId=sent[-1]["id"])
                atomic_save_queue(path, queue)
                print(json.dumps({"account": account, "workId": row.get("workId") or row["id"],
                                  "page": page_number, "generationStatus": row["generationStatus"],
                                  "intentId": intent["id"],
                                  "startMessageId": sent[-1]["id"]}, ensure_ascii=False), flush=True)
                return 0
            time.sleep(8)
        raise SubmissionOutcomeUnknown("send receipt not found; durable intent retained; reconcile only, never resend")
    except Exception as error:
        if row.get("submissionIntent"):
            row["submissionIntent"].update(lastError=str(error)[:240], reconcileOnly=True)
            row["generationStatus"] = "SUBMISSION_UNKNOWN"
            atomic_save_queue(path, queue)
        else:
            row["startError"] = str(error)[:240]
            atomic_save_queue(path, queue)
        print(json.dumps({"account": account, "workId": row.get("workId") or row["id"],
                          "error": str(error),
                          "next": "read-only reconcile before any retry"}, ensure_ascii=False), flush=True)
        return 2
    finally:
        page.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", required=True)
    parser.add_argument("--adapter")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--page", type=int)
    parser.add_argument("--work-id", help="require this exact workId; never fall through to another queue row")
    parser.add_argument("--intent-id", help="reuse the supervisor's durable write-ahead intent id")
    parser.add_argument("--lock-already-held", action="store_true",
                        help="use only when called inside production_supervisor's account lock")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    path = Path(args.queue)
    if args.lock_already_held:
        return _run_locked(args)
    try:
        account = json.loads(path.read_text(encoding="utf-8-sig"))["account"]
        lock_path = path.parent / "supervisor" / f"{account}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with account_lock(lock_path):
            return _run_locked(args)
    except Exception as error:
        print(json.dumps({"error": str(error), "next": "reconcile; do not resend"},
                         ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
