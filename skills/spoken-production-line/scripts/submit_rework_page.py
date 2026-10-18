"""Submit one page-level template repair through an existing authenticated CDP tab.

This is intentionally fail-closed: every existing ChatGPT page is inspected before
navigation, a durable write-ahead intent is saved before clicking Send, and an
unresolved intent is reconciled read-only on every later run.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
import urllib.request

from cdp_adapter import CDP, api_snapshot, visible_state
from production_supervisor import account_lock


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def norm(text: str | None) -> str:
    return " ".join((text or "").split())


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp and temp.exists():
            temp.unlink()


def is_active_generation(page: CDP) -> bool:
    return bool(page.evaluate(
        "[...document.querySelectorAll('button')].some(b=>"
        "/^(停止|Stop|Stop generating)$/.test(b.getAttribute('aria-label')||'')"
        "&&b.getBoundingClientRect().width>0)"
    ))


def visible_quota_notice(page: CDP) -> str:
    return page.evaluate(
        "(()=>{const t=document.body?.innerText||'';return t.match(/"
        "图片生成次数已用完|图像生成次数已用完|上传次数已达上限|"
        "你已达到文件附件数量限制|You've hit your image generation limit|"
        "image generation limit|upload limit reached/i)?.[0]||''})()"
    ) or ""


def inspect_pages(port: int, core=CDP, ui=visible_state):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.load(response)
    pages = [t for t in targets if t.get("type") == "page"
             and t.get("url", "").startswith("https://chatgpt.com/")]
    if not pages:
        raise RuntimeError("No existing authenticated ChatGPT page; refusing profile creation")
    for target in pages:
        page = core(target["webSocketDebuggerUrl"], timeout=10)
        try:
            state = ui(page)
            if is_active_generation(page):
                raise RuntimeError("A ChatGPT tab is generating; preserve every page")
            if state.get("removers") or any(norm(e.get("text")) for e in state.get("editors", [])):
                raise RuntimeError("A ChatGPT tab has a draft or attachments; preserve every page")
        finally:
            page.close()
    return pages


def wait_for_conversation(page: CDP, expected_url: str, timeout: int = 90, ui=visible_state,
                          sleep=time.sleep):
    """Wait for the exact target route, not merely the old page's still-live composer."""
    deadline = time.monotonic() + timeout
    last_state = None
    while time.monotonic() < deadline:
        last_state = ui(page)
        actual = (last_state.get("url") or "").split("?", 1)[0]
        if actual == expected_url and last_state.get("ready") == "complete" and last_state.get("editors"):
            return last_state
        sleep(1)
    actual = (last_state or {}).get("url", "")
    raise TimeoutError(f"Exact conversation did not become ready; expected {expected_url}, observed {actual}")


def insert_prompt(page: CDP, prompt: str, ui=visible_state) -> None:
    chunks = [prompt[i:i + 900] for i in range(0, len(prompt), 900)]
    for chunk in chunks:
        encoded = json.dumps(chunk, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        result = page.evaluate(
            "(()=>{const c=document.querySelector('#prompt-textarea')||"
            "document.querySelector('div[contenteditable=true][role=textbox]')||"
            "document.querySelector('[role=textbox][contenteditable=true]');"
            f"if(!c)return -1;c.focus();const ok=document.execCommand('insertText',false,{encoded});"
            "return ok?((c.innerText!==undefined?c.innerText:c.value)||'').length:-2})()"
        )
        if not isinstance(result, int) or result < 0:
            raise RuntimeError("Renderer rejected prompt insertion")
    state = ui(page)
    if not any(norm(e.get("text")) == norm(prompt) for e in state.get("editors", [])):
        raise RuntimeError("Composer does not contain the exact repair prompt")
    ready = page.evaluate(
        "(()=>{const b=document.querySelector('button[data-testid=send-button]')||"
        "[...document.querySelectorAll('button')].find(x=>"
        "/^(发送|发送提示词|Send prompt)$/.test(x.getAttribute('aria-label')||''));"
        "return !!b&&b.getBoundingClientRect().width>0&&!b.disabled})()"
    )
    if not ready:
        raise RuntimeError("Send button is not enabled; prompt remains unsent")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--account", choices=["A", "B"], required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--page", required=True)
    parser.add_argument("--conversation-url", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    root = Path(args.task_root)
    prompt_path = Path(args.prompt)
    intent_path = Path(args.intent)
    receipt_path = Path(args.receipt)
    prompt = prompt_path.read_text(encoding="utf-8-sig").strip()
    if not prompt or len(prompt) < 80:
        raise ValueError("Repair prompt is empty or implausibly short")
    expected = args.conversation_url.split("?", 1)[0]
    parsed = urlparse(expected)
    match = re.search(r"/c/([A-Za-z0-9-]+)$", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "chatgpt.com" or not match:
        raise ValueError("Expected an exact ChatGPT conversation URL")
    conversation_id = match.group(1)
    port = args.port or (9431 if args.account == "A" else 9432)
    lock = root / "supervisor" / f"{args.account}.lock"

    with account_lock(lock):
        existing_intent = json.loads(intent_path.read_text(encoding="utf-8")) if intent_path.exists() else None
        if existing_intent:
            # An intent with no conclusive receipt is never re-submitted.
            targets = inspect_pages(port)
            exact = next((t for t in targets if t.get("url", "").split("?", 1)[0] == expected), None)
            if not exact:
                print(json.dumps({"status": "SUBMISSION_UNKNOWN", "next": "read-only reconcile; do not resend"}, ensure_ascii=False))
                return 2
            page = CDP(exact["webSocketDebuggerUrl"], timeout=10)
            try:
                snapshot = api_snapshot(page)
                if not snapshot or not snapshot.get("ok") or snapshot.get("id") != conversation_id:
                    raise RuntimeError("Conversation readback failed")
                matches = [m for m in snapshot.get("messages", [])
                           if m.get("role") == "user" and norm(m.get("text")) == norm(prompt)]
                if matches:
                    receipt = {"status": "submitted_readback_verified", "workId": args.work_id,
                               "account": args.account, "page": args.page,
                               "conversationUrl": expected, "messageId": matches[-1]["id"],
                               "messageStatus": matches[-1].get("status"), "observedAt": now_utc(),
                               "promptSha256": existing_intent["promptSha256"]}
                    atomic_json(receipt_path, receipt)
                    print(json.dumps(receipt, ensure_ascii=False))
                    return 0
                print(json.dumps({"status": "SUBMISSION_UNKNOWN", "next": "read-only reconcile; do not resend"}, ensure_ascii=False))
                return 2
            finally:
                page.close()

        targets = inspect_pages(port)
        target = next((t for t in targets if t.get("url", "").split("?", 1)[0] == expected), targets[0])
        page = CDP(target["webSocketDebuggerUrl"], timeout=10)
        try:
            if target.get("url", "").split("?", 1)[0] != expected:
                page.call("Page.navigate", {"url": expected}, timeout=15)
            state = wait_for_conversation(page, expected)
            if state.get("quotaNotice") or visible_quota_notice(page):
                raise RuntimeError("Visible platform limit notice; no submission")
            if len(state.get("editors", [])) != 1 or norm(state["editors"][0].get("text")):
                raise RuntimeError("Target composer is not uniquely blank")
            if is_active_generation(page):
                raise RuntimeError("Target conversation is generating; preserve it")

            snapshot = api_snapshot(page)
            if not snapshot or not snapshot.get("ok") or snapshot.get("id") != conversation_id:
                raise RuntimeError("Exact conversation identity not verified")
            duplicate = [m for m in snapshot.get("messages", [])
                         if m.get("role") == "user" and norm(m.get("text")) == norm(prompt)]
            if duplicate:
                receipt = {"status": "already_submitted_readback_verified", "workId": args.work_id,
                           "account": args.account, "page": args.page, "conversationUrl": expected,
                           "messageId": duplicate[-1]["id"], "observedAt": now_utc(),
                           "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
                atomic_json(receipt_path, receipt)
                print(json.dumps(receipt, ensure_ascii=False))
                return 0
            tail = next((m for m in reversed(snapshot.get("messages", [])) if m.get("role") == "assistant"), None)
            if not tail or tail.get("status") not in {"finished_successfully", "finished_partial"}:
                raise RuntimeError("Latest assistant turn is not terminal; no submission")

            insert_prompt(page, prompt)
            intent = {"status": "SUBMISSION_UNKNOWN", "reconcileOnly": True,
                      "workId": args.work_id, "account": args.account, "page": args.page,
                      "conversationUrl": expected, "promptPath": str(prompt_path),
                      "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                      "createdAt": now_utc(), "clickIssuedAt": None}
            atomic_json(intent_path, intent)
            page.evaluate(
                "(()=>{const b=document.querySelector('button[data-testid=send-button]')||"
                "[...document.querySelectorAll('button')].find(x=>"
                "/^(发送|发送提示词|Send prompt)$/.test(x.getAttribute('aria-label')||''));"
                "if(!b||b.disabled)throw Error('send button unavailable');b.click();return true})()"
            )
            intent["clickIssuedAt"] = now_utc()
            intent["status"] = "SEND_CLICKED_READBACK_PENDING"
            atomic_json(intent_path, intent)

            before_ids = {m.get("id") for m in snapshot.get("messages", [])}
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                after = api_snapshot(page)
                if after and after.get("ok"):
                    sent = [m for m in after.get("messages", []) if m.get("id") not in before_ids
                            and m.get("role") == "user" and norm(m.get("text")) == norm(prompt)]
                    if sent:
                        receipt = {"status": "submitted_readback_verified", "workId": args.work_id,
                                   "account": args.account, "page": args.page,
                                   "conversationUrl": expected, "messageId": sent[-1]["id"],
                                   "messageStatus": sent[-1].get("status"), "observedAt": now_utc(),
                                   "promptSha256": intent["promptSha256"]}
                        atomic_json(receipt_path, receipt)
                        print(json.dumps(receipt, ensure_ascii=False))
                        return 0
                time.sleep(4)
            print(json.dumps({"status": "SUBMISSION_UNKNOWN", "next": "read-only reconcile; do not resend"}, ensure_ascii=False))
            return 2
        finally:
            page.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error": str(error), "next": "preserve state; reconcile before any retry"}, ensure_ascii=False))
        raise SystemExit(2)
