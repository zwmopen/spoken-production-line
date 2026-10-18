"""Account-scoped rolling work-set telemetry; no fixed suite cap."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
import tempfile

UTC8 = dt.timezone(dt.timedelta(hours=8))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def now_iso() -> str:
    return dt.datetime.now(UTC8).isoformat()


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        value = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC8)
        return parsed.astimezone(UTC8)
    except (TypeError, ValueError):
        return None


def read(path: Path) -> dict:
    if not path.exists():
        return {
            "schemaVersion": 1,
            "windowMinutes": 180,
            "fixedCapEnabled": False,
            "countKey": "account+workId",
            "completedAnchor": "acceptedAt",
            "unfinishedAnchor": "startedAt",
            "events": [],
        }
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("invalid work-set ledger")
    data.pop("maxPerAccount", None)
    data["fixedCapEnabled"] = False
    return data


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updatedAt"] = now_iso()
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def anchor(event: dict) -> dt.datetime | None:
    if event.get("status") in {"completed", "accepted", "accepted_notified"}:
        return parse_time(event.get("acceptedAt") or event.get("completedAt") or event.get("windowAnchor"))
    return parse_time(event.get("startedAt") or event.get("windowAnchor"))


def recent_for(data: dict, account: str, now: dt.datetime) -> list[dict]:
    cutoff = now - dt.timedelta(minutes=int(data.get("windowMinutes", 180)))
    unique: dict[str, dict] = {}
    for event in data["events"]:
        if event.get("account") != account or not event.get("workId"):
            continue
        wid = str(event["workId"])
        old = unique.get(wid)
        if old is None or (parse_time(event.get("startedAt")) or cutoff) > (parse_time(old.get("startedAt")) or cutoff):
            unique[wid] = event
    out = []
    for event in unique.values():
        at = anchor(event)
        # An active or uncertain started work with no usable timestamp is a
        # conservative occupancy, not silently free capacity.
        if at is None and event.get("status") not in {"completed", "accepted", "accepted_notified"}:
            out.append(event)
        elif at is not None and at >= cutoff:
            out.append(event)
    return sorted(out, key=lambda e: (anchor(e) or now, str(e.get("workId"))))


def status(data: dict, account: str, now: dt.datetime) -> dict:
    recent = recent_for(data, account, now)
    return {
        "account": account,
        "windowMinutes": int(data.get("windowMinutes", 180)),
        "fixedCapEnabled": False,
        "usedSets": len(recent),
        "remainingStarts": None,
        "canStartBySetCount": True,
        "workIds": [e.get("workId") for e in recent],
        "asOf": now.isoformat(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["status", "reserve", "complete", "fail"])
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--account", required=True)
    ap.add_argument("--work-id", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--template-id", default="")
    ap.add_argument("--planned-pages", type=int, default=0)
    ap.add_argument("--message-id", default="")
    ap.add_argument("--product-path", default="")
    ap.add_argument("--reason", default="")
    args = ap.parse_args()

    data = read(args.ledger)
    now = dt.datetime.now(UTC8)
    if args.command == "status":
        print(json.dumps(status(data, args.account, now), ensure_ascii=False, indent=2))
        return 0
    if not args.work_id:
        ap.error("--work-id is required for write operations")

    found = next((e for e in data["events"] if e.get("account") == args.account and e.get("workId") == args.work_id), None)
    if args.command == "reserve":
        if found:
            print(json.dumps({"reserved": True, "idempotent": True, "event": found, "status": status(data, args.account, now)}, ensure_ascii=False, indent=2))
            return 0
        current = status(data, args.account, now)
        found = {
            "account": args.account,
            "workId": args.work_id,
            "title": args.title,
            "templateId": args.template_id,
            "status": "in_progress",
            "startedAt": now.isoformat(),
            "windowAnchor": now.isoformat(),
            "plannedPages": args.planned_pages,
            "startMessageId": args.message_id or None,
        }
        data["events"].append(found)
        write_atomic(args.ledger, data)
        print(json.dumps({"reserved": True, "idempotent": False, "event": found, "status": status(data, args.account, now)}, ensure_ascii=False, indent=2))
        return 0

    if not found:
        raise SystemExit("workId not reserved; refusing to mutate the set ledger")
    if args.command == "complete":
        found.update({"status": "completed", "acceptedAt": now.isoformat(), "windowAnchor": now.isoformat(), "productPath": args.product_path or found.get("productPath"), "acceptedPages": args.planned_pages or found.get("acceptedPages"), "deliveryReceipt": args.message_id or found.get("deliveryReceipt")})
    else:
        found.update({"status": "failed", "failedAt": now.isoformat(), "failureReason": args.reason, "windowAnchor": found.get("startedAt")})
    write_atomic(args.ledger, data)
    print(json.dumps({"updated": True, "event": found, "status": status(data, args.account, now)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
