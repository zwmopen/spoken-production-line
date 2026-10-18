#!/usr/bin/env python3
"""Read, validate, and save one completed COPY_FORMAT:MULTI reply."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

from cdp_adapter import CDP, api_snapshot


def unwrap_text_block(raw: str) -> str:
    """Extract a text payload from either supported server rendering form."""
    raw = raw.strip()
    if raw.startswith("```text") and raw.endswith("```"):
        return raw[len("```text"): -len("```")].strip("\r\n ")
    opening = '<CodeBlock language="text">'
    closing = "</CodeBlock>"
    if raw.startswith(opening) and raw.endswith(closing) and \
            raw.count(opening) == 1 and raw.count(closing) == 1:
        return raw[len(opening): -len(closing)].strip("\r\n ")
    raise ValueError("expected one text code block in a supported server format")


def validate_copy_body(body: str) -> dict:
    """Validate the exact three-version publishing contract without network I/O."""
    expected = ["小红书1｜自然攻略版", "小红书2｜大纲方案版", "抖音｜攻略版"]
    starts = re.findall(r"<<<VERSION_START:([^>]+)>>>\s*", body)
    ends = re.findall(r"<<<VERSION_END>>>\s*", body)
    sections = re.findall(
        r"<<<VERSION_START:([^>]+)>>>\s*(.*?)\s*<<<VERSION_END>>>", body, re.S
    )
    if "<<<COPY_FORMAT:MULTI>>>" not in body or starts != expected or len(ends) != 3:
        raise ValueError(f"version contract mismatch: starts={starts}, ends={len(ends)}")
    if [name for name, _ in sections] != expected:
        raise ValueError("version section boundaries do not match the required order")
    separator_lines = [line for line in body.splitlines() if "\u2800" in line]
    if len(separator_lines) < 2 or any(line.strip() != "\u2800" for line in separator_lines):
        raise ValueError("U+2800 separators must appear on standalone lines")
    if re.search(r"\(（标题、正文|此处输出|占位|placeholder\)", body, re.I):
        raise ValueError("placeholder text remains")

    ranges = [(650, 850), (700, 900), (450, 650)]
    counts, title_lengths, topic_counts = [], [], []
    for (name, section), (minimum, maximum) in zip(sections, ranges):
        lines = [line for line in section.splitlines() if line.strip() != "\u2800"]
        visible_text = "".join(lines)
        title = next((line.strip() for line in lines if line.strip()), "")
        topics = re.findall(r"#[^\s#]+", section)
        counts.append(len(visible_text))
        title_lengths.append(len(title))
        topic_counts.append(len(topics))
        if len(title) > 20:
            raise ValueError(f"{name} title exceeds 20 characters: {len(title)}")
        if not minimum <= len(visible_text) <= maximum:
            raise ValueError(f"{name} publish character count {len(visible_text)} outside {minimum}-{maximum}")
        if name.startswith("小红书") and not 8 <= len(topics) <= 10:
            raise ValueError(f"{name} topic count {len(topics)} outside 8-10")
        if name == "抖音｜攻略版" and len(topics) != 5:
            raise ValueError(f"Douyin topic count must be exactly 5, got {len(topics)}")

    return {
        "versionNames": starts,
        "versionCount": len(starts),
        "u2800Separators": len(separator_lines),
        "publishCharacterCounts": counts,
        "titleCharacterCounts": title_lengths,
        "topicCounts": topic_counts,
    }


def add_version_note(body: str, version_name: str, note: str) -> str:
    """Apply one explicit, idempotent editorial note before that version's tags."""
    if version_name not in {"小红书1｜自然攻略版", "小红书2｜大纲方案版", "抖音｜攻略版"}:
        raise ValueError("unknown version for editorial note")
    note = note.strip()
    if not note or "\n" in note or "<<<" in note or "#" in note:
        raise ValueError("editorial note must be one plain, non-hashtag line")
    start = f"<<<VERSION_START:{version_name}>>>"
    if body.count(start) != 1:
        raise ValueError("target version marker must occur exactly once")
    prefix, section_with_end = body.split(start, 1)
    section, suffix = section_with_end.split("<<<VERSION_END>>>", 1)
    if note not in section:
        lines = section.splitlines()
        tag_line = next((i for i, line in enumerate(lines) if any(part.startswith("#") for part in line.split())), None)
        if tag_line is None:
            raise ValueError("target version has no topic line for a safe note insertion")
        lines.insert(tag_line, note)
        section = "\n".join(lines)
    return prefix + start + section + "<<<VERSION_END>>>" + suffix


def write_copy_payload(output: Path, payload: str) -> str:
    """Write exact UTF-8 bytes and preserve idempotency across Windows newlines."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.read_text(encoding="utf-8") != payload:
            raise ValueError("target copy file exists with different content; refusing to overwrite")
        return "already_same"
    output.write_bytes(payload.encode("utf-8"))
    return "created"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--conversation-id", required=True)
    ap.add_argument("--message-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--work-id", default="")
    ap.add_argument("--account", default="")
    ap.add_argument("--template-id", default="")
    ap.add_argument("--append-note-version", choices=["小红书1｜自然攻略版", "小红书2｜大纲方案版", "抖音｜攻略版"])
    ap.add_argument("--append-note", default="")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    pages = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{args.port}/json/list", timeout=5
    ))
    target = next((x for x in pages if x.get("type") == "page" and
                   args.conversation_id in x.get("url", "")), None)
    if not target:
        raise SystemExit("matching conversation page not found")
    page = CDP(target["webSocketDebuggerUrl"], timeout=15)
    try:
        snapshot = api_snapshot(page)
    finally:
        page.close()
    if not snapshot or not snapshot.get("ok") or snapshot.get("id") != args.conversation_id:
        raise SystemExit("conversation identity did not verify")
    message = next((m for m in snapshot.get("messages", [])
                    if m.get("id") == args.message_id), None)
    if not message or message.get("role") != "assistant":
        raise SystemExit("specified assistant reply not found")
    if message.get("status") != "finished_successfully":
        raise SystemExit("copy reply is not terminal")

    raw = message.get("text") or ""
    try:
        server_body = unwrap_text_block(raw)
        body = server_body
        if bool(args.append_note_version) != bool(args.append_note.strip()):
            raise ValueError("append-note-version and append-note must be supplied together")
        if args.append_note_version:
            body = add_version_note(body, args.append_note_version, args.append_note)
        validation = validate_copy_body(body)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output = Path(args.out)
    payload = body + "\n"
    try:
        write_status = write_copy_payload(output, payload)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    physical_bytes = output.read_bytes()
    receipt = {
        "workId": args.work_id,
        "account": args.account,
        "templateId": args.template_id,
        "conversationId": args.conversation_id,
        "messageId": args.message_id,
        "status": message["status"],
        "readbackAt": __import__("datetime").datetime.now(
            __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))
        ).isoformat(),
        "textPath": str(output),
        "sha256": hashlib.sha256(physical_bytes).hexdigest(),
        "normalizedTextSha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "byteLength": len(physical_bytes),
        "writeStatus": write_status,
        "serverBodySha256": hashlib.sha256(server_body.encode("utf-8")).hexdigest(),
        "editorialAdjustments": ([{"version": args.append_note_version, "note": args.append_note.strip()}]
                                 if args.append_note_version else []),
        **validation,
        "douyinTopicCount": validation["topicCounts"][2],
        "contractValidated": True,
    }
    Path(args.receipt).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
