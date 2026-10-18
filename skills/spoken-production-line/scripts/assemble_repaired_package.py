from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from PIL import Image

PARENT_WORK_ID = "MS-TONGLU-004-FULL-REPAIR-01"
REPAIR_WORK_ID = "MS-TONGLU-004-TEMPLATE-REPAIR-02"
TEMPLATE_PAGE = {1: 1, 2: 2, 3: 3, 4: 4, 5: 2, 6: 4, 7: 2, 8: 4}
SOURCE_MAP = {
    1: ["封面", "内页3", "内页7", "内页12", "内页13"],
    2: ["内页8", "内页6", "内页9"],
    3: ["内页3", "内页2", "内页5", "内页4"],
    4: ["内页10", "内页9", "内页7"],
    5: ["内页8", "内页1"],
    6: ["内页12"],
    7: ["内页13"],
    8: ["内页5", "内页11"],
}
EXPECTED_COPY_NAMES = [
    "小红书1｜自然攻略版",
    "小红书2｜大纲方案版",
    "抖音｜攻略版",
]


def now_local() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")


def validate_copy_text(text: str) -> list[dict]:
    if text.count("<<<COPY_FORMAT:MULTI>>>") != 1:
        raise ValueError("copy must contain exactly one COPY_FORMAT:MULTI marker")
    pattern = re.compile(r"<<<VERSION_START:([^>]+)>>>s*(.*?)s*<<<VERSION_END>>>", re.S)
    blocks = [{"name": m.group(1).strip(), "body": m.group(2).strip()} for m in pattern.finditer(text)]
    if [b["name"] for b in blocks] != EXPECTED_COPY_NAMES:
        raise ValueError("copy version names or order do not match the shared contract")
    if text.count("<<<VERSION_START:") != 3 or text.count("<<<VERSION_END>>>") != 3:
        raise ValueError("copy must contain exactly three closed versions")
    for block in blocks:
        if not block["body"] or "此处输出" in block["body"] or "占位" in block["body"]:
            raise ValueError("copy contains an empty version or placeholder")
    if "⠀" not in text:
        raise ValueError("copy lacks the required U+2800 paragraph separator")
    douyin_tags = re.findall(r"(?<!S)#[^s#]+", blocks[2]["body"])
    if len(douyin_tags) > 5:
        raise ValueError(f"douyin version has {len(douyin_tags)} hashtags; maximum is five")
    return blocks


def inspect_image(path: Path) -> dict:
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError(f"unsupported image format for {path}")
        if min(width, height) < 500 or abs(width / height - 0.75) > 0.005:
            raise ValueError(f"image is not a full 3:4 native page: {path} ({width}x{height})")
    return {"width": width, "height": height, "sha256": sha256(path), "bytes": path.stat().st_size}


def source_files_for(source_root: Path, prefixes: list[str]) -> list[Path]:
    resolved = []
    for prefix in prefixes:
        matches = sorted(p for p in source_root.glob(prefix + "-*.jpg") if p.is_file())
        if len(matches) != 1:
            raise ValueError(f"expected exactly one source for {prefix}, found {[p.name for p in matches]}")
        resolved.append(matches[0])
    return resolved


def build_page_records(root: Path, source_root: Path, template_root: Path) -> list[dict]:
    records = []
    repair = {
        1: (root / "A-Tonglu-template-rework-P1-download" / "P1.png",
            root / "A-Tonglu-template-rework-P1-视觉验收.json",
            root / "A-Tonglu-template-rework-P1-提交回执.json",
            root / "A-Tonglu-template-rework-P1-assets.json"),
        3: (root / "A-Tonglu-template-rework-P3-download" / "P1.png",
            root / "A-Tonglu-template-rework-P3-视觉验收.json",
            root / "A-Tonglu-template-rework-P3-提交回执.json",
            root / "A-Tonglu-template-rework-P3-assets.json"),
    }
    for page in range(1, 9):
        if page in repair:
            image_path, qa_path, submission_path, assets_path = repair[page]
            qa = load_json(qa_path)
            submission = load_json(submission_path)
            assets = json.loads(assets_path.read_text(encoding="utf-8-sig"))
            if qa.get("accepted") is not True or qa.get("page") != f"P{page}":
                raise ValueError(f"repair page P{page} is not accepted")
            if submission.get("status") != "submitted_readback_verified":
                raise ValueError(f"repair page P{page} submission is not readback verified")
            if len(assets) != 1 or assets[0] != qa.get("assetId"):
                raise ValueError(f"repair page P{page} must bind exactly one native asset")
            if qa.get("startMessageId") != submission.get("messageId"):
                raise ValueError(f"repair page P{page} message IDs disagree")
            asset_id = assets[0]
            assistant_id = qa.get("terminalAssistantMessageId")
            acceptance_path = qa_path
        else:
            image_path = root / f"A-Tonglu-P{page}-candidate.png"
            qa_path = root / f"A-Tonglu-P{page}逐页视觉预审.json"
            native_path = root / f"A-Tonglu-P{page}原生下载回执.json"
            submission_path = root / f"A-Tonglu-P{page}提交回执.json"
            qa = load_json(qa_path)
            native = load_json(native_path)
            submission = load_json(submission_path)
            if qa.get("status") != "PASS":
                raise ValueError(f"historical page P{page} lacks a passing visual receipt")
            if not image_path.is_file() or sha256(image_path) != qa.get("sha256"):
                raise ValueError(f"historical page P{page} image no longer matches its accepted SHA")
            asset_id = native.get("assetId")
            assistant_id = native.get("assistantId")
            acceptance_path = qa_path
            if not asset_id:
                raise ValueError(f"historical page P{page} is missing its native asset ID")
        if not image_path.is_file():
            raise FileNotFoundError(image_path)
        info = inspect_image(image_path)
        template_image = template_root / f"模板-P{TEMPLATE_PAGE[page]}.jpg"
        if not template_image.is_file():
            raise FileNotFoundError(template_image)
        sources = source_files_for(source_root, SOURCE_MAP[page])
        records.append({
            "page": page,
            "sourceTaskId": PARENT_WORK_ID,
            "repairWorkId": REPAIR_WORK_ID if page in repair else None,
            "sourceMappingLabels": SOURCE_MAP[page],
            "sourceFiles": [{"path": str(p), "name": p.name, "sha256": sha256(p)} for p in sources],
            "stagingPath": str(image_path),
            "assetId": asset_id,
            "startMessageId": submission.get("messageId"),
            "terminalAssistantMessageId": assistant_id,
            "submissionReceiptPath": str(submission_path),
            "nativeDownloadReceiptPath": str(root / f"A-Tonglu-P{page}原生下载回执.json") if page not in repair else None,
            "acceptanceReceiptPath": str(acceptance_path),
            "templateImagePath": str(template_image),
            "templateImageSha256": sha256(template_image),
            "acceptanceStatus": "PASS",
            **info,
        })
    hashes = [p["sha256"] for p in records]
    if len(set(hashes)) != 8:
        raise ValueError("page images are not eight unique files")
    return records


def preflight(root: Path, requested_output: Path | None = None) -> dict:
    ledger = load_json(root / "生产台账.json")
    queue = load_json(root / "queue-A.json")
    parent = next((r for r in ledger["records"] if r.get("workId") == PARENT_WORK_ID), None)
    child = next((r for r in ledger["records"] if r.get("workId") == REPAIR_WORK_ID), None)
    parent_task = next((r for r in queue["tasks"] if r.get("workId") == PARENT_WORK_ID), None)
    if not parent or not child or not parent_task:
        raise ValueError("parent/repair work records are missing")
    source_root = Path(parent["sourceFolderPath"])
    template_root = Path(parent["localTemplatePath"])
    if not source_root.is_dir() or not template_root.is_dir():
        raise ValueError("source or selected template directory does not exist")
    actual_sources = sorted(source_root.glob("*.jpg"))
    if len(actual_sources) != 15 or not (source_root / "文案.txt").is_file():
        raise ValueError("source must contain the verified 15 JPGs and 文案.txt")
    source_metadata = Path(parent_task.get("sourceMetadataPath") or "")
    metadata_event = None
    if source_metadata.is_file():
        meta = load_json(source_metadata)
        metadata_event = next((x for x in meta.get("usageEvents", []) if x.get("workId") == PARENT_WORK_ID), None)
    if metadata_event is None:
        raise ValueError("idempotent source usage event for the parent workId is missing")
    copy_source = root / "A-Tonglu-文案.txt"
    copy_text = copy_source.read_text(encoding="utf-8-sig")
    copy_blocks = validate_copy_text(copy_text)
    pages = build_page_records(root, source_root, template_root)
    output_root = Path(parent["productFolderPath"]).parent
    if requested_output:
        output = requested_output
    else:
        stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        output = output_root / f"{stamp} GPT a-桐庐江景包栋小院团建_T88修复版"
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing package: {output}")
    copy_receipt = load_json(root / "A-Tonglu文案回读.json")
    if copy_receipt.get("status") != "finished_successfully":
        raise ValueError("three-copy server reply is not terminal")
    old_notification = load_json(root / "A-Tonglu飞书落地通知.json")
    if old_notification.get("readbackVerified") is not True:
        raise ValueError("prior notification history is not verifiable")
    return {
        "parent": parent,
        "child": child,
        "parentTask": parent_task,
        "sourceRoot": source_root,
        "templateRoot": template_root,
        "sourceFiles": actual_sources + [source_root / "文案.txt"],
        "sourceMetadataPath": source_metadata,
        "sourceUsageEvent": metadata_event,
        "copySource": copy_source,
        "copyText": copy_text,
        "copyBlocks": copy_blocks,
        "copySha256": sha256(copy_source),
        "copyReceipt": copy_receipt,
        "oldNotification": old_notification,
        "pages": pages,
        "output": output,
    }


def assemble(root: Path, commit: bool = False, output: Path | None = None) -> dict:
    data = preflight(root, output)
    target = data["output"]
    timestamp = now_local()
    template_title = data["parent"].get("templateTitle")
    conversation_url = data["parentTask"].get("conversationUrl")
    manifest = {
        "schemaVersion": 3,
        "metadataSchemaVersion": "3.0",
        "workId": PARENT_WORK_ID,
        "repairWorkIds": [REPAIR_WORK_ID],
        "sourceTaskId": data["parent"].get("sourceTaskId") or PARENT_WORK_ID,
        "account": "A",
        "title": data["parent"].get("title"),
        "status": "archiving" if commit else "dry_run_validated",
        "deliveryStatus": "archiving" if commit else "dry_run_validated",
        "productionMode": "template_migration",
        "localFolderPath": str(target),
        "sourceFolderPath": str(data["sourceRoot"]),
        "sourceFiles": [{"path": str(p), "name": p.name, "sha256": sha256(p)} for p in data["sourceFiles"]],
        "templateId": "T88",
        "templateTitle": template_title,
        "localTemplatePath": str(data["templateRoot"]),
        "onlineTemplateConversationUrl": data["parent"].get("templateLearningConversationUrl"),
        "productionConversationUrl": conversation_url,
        "conversationId": "6ac9db69-e054-83ee-9cf3-0884c716256a",
        "planMessageId": data["parent"].get("planMessageId"),
        "supersededPlanPath": str(root / "A-Tonglu-真实计划.txt"),
        "planCorrection": "旧计划误将真实T88-P3写成五图片区；返修按原P3真实四等分2x2结构生成并验收。",
        "sourceIdentity": data["parent"].get("sourceIdentity"),
        "plannedPageCount": 8,
        "actualPageCount": 8,
        "acceptedPageCount": 8,
        "pages": data["pages"],
        "copy": {
            "path": str(target / "文案.txt"),
            "sourcePath": str(data["copySource"]),
            "sha256": data["copySha256"],
            "versions": [b["name"] for b in data["copyBlocks"]],
            "serverReplyId": data["copyReceipt"].get("assistantId"),
            "sourceReplyReceiptPath": str(root / "A-Tonglu文案回读.json"),
        },
        "sourceUsage": {
            "idempotencyKey": PARENT_WORK_ID,
            "existingUsageEvent": True,
            "usageCountIncremented": False,
            "sourceMetadataPath": str(data["sourceMetadataPath"]),
        },
        "priorNotification": {
            "messageId": data["oldNotification"].get("messageId"),
            "readbackVerified": True,
            "priorAcceptanceInvalidated": True,
            "correctionNotificationStatus": "pending",
        },
        "timestamps": {
            "assembledAt": timestamp if commit else None,
            "generatedAt": None,
            "acceptanceTimeSource": "page-level server receipts and local visual review receipts; exact generation time only when recorded in those receipts",
        },
        "imageProvenance": "ChatGPT Web native image generation; existing accepted native PNGs copied byte-for-byte; no code cropping or recomposition",
        "safety": {
            "newOutputDirectory": True,
            "oldPackagePreserved": True,
            "oldRejectedImagesExcluded": True,
            "noDuplicateSourceUsage": True,
        },
    }
    if not commit:
        return {
            "status": "DRY_RUN_PASS",
            "output": str(target),
            "pages": [{"page":p["page"],"sha256":p["sha256"],"width":p["width"],"height":p["height"],"assetId":p["assetId"]} for p in data["pages"]],
            "copyVersions": [b["name"] for b in data["copyBlocks"]],
            "copySha256": data["copySha256"],
            "sourceImageCount": len(data["sourceFiles"]) - 1,
            "sourceUsageEvent": PARENT_WORK_ID,
            "wouldIncrementUsage": False,
        }
    target.mkdir(parents=False, exist_ok=False)
    manifest_path = target / "manifest.json"
    save_json(manifest_path, manifest)
    copied_pages = []
    for page in data["pages"]:
        dest = target / f"P{page['page']}.png"
        if dest.exists():
            raise FileExistsError(dest)
        shutil.copy2(page["stagingPath"], dest)
        info = inspect_image(dest)
        if info["sha256"] != page["sha256"] or info["width"] != page["width"] or info["height"] != page["height"]:
            raise ValueError(f"copied page verification failed: {dest}")
        copied_pages.append({"page":page["page"],"path":str(dest),"sha256":info["sha256"],"width":info["width"],"height":info["height"],"status":"PASS"})
    copy_dest = target / "文案.txt"
    shutil.copy2(data["copySource"], copy_dest)
    if sha256(copy_dest) != data["copySha256"] or validate_copy_text(copy_dest.read_text(encoding="utf-8-sig")) != data["copyBlocks"]:
        raise ValueError("copy byte or contract verification failed")
    qa = {
        "schemaVersion": 3,
        "workId": PARENT_WORK_ID,
        "repairWorkIds": [REPAIR_WORK_ID],
        "reviewer": "Codex",
        "reviewedAt": timestamp,
        "expectedPageCount": 8,
        "acceptedPageCount": 8,
        "pages": copied_pages,
        "passed": True,
        "reviewBasis": "Each original accepted page receipt was SHA-checked; replacement P1/P3 were newly generated, downloaded, and visually compared with the actual T88 masters."
    }
    qa_path = target / "逐页视觉验收V3.json"
    save_json(qa_path, qa)
    manifest["pages"] = [
        {**p, "path":str(target / f"P{p['page']}.png")} for p in data["pages"]
    ]
    manifest["copy"]["path"] = str(copy_dest)
    manifest["acceptanceReceiptPath"] = str(qa_path)
    manifest["status"] = "accepted_pending_correction_notification"
    manifest["deliveryStatus"] = "accepted_pending_correction_notification"
    manifest["timestamps"]["archivedAt"] = timestamp
    save_json(manifest_path, manifest)
    package_receipt = {
        "workId": PARENT_WORK_ID,
        "repairWorkId": REPAIR_WORK_ID,
        "status": "accepted_pending_correction_notification",
        "createdAt": timestamp,
        "productPath": str(target),
        "manifestPath": str(manifest_path),
        "acceptancePath": str(qa_path),
        "physicalImages": 8,
        "acceptedPages": 8,
        "copyVersions": len(data["copyBlocks"]),
        "copyPath": str(copy_dest),
        "sourceUsageEventIdempotent": True,
        "sourceUsageCountIncremented": False,
        "oldRejectedOutputPreserved": True,
    }
    save_json(root / "A-Tonglu-repaired-package-组装回执.json", package_receipt)
    return package_receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble the repaired, verified Tonglu T88 package without overwriting prior output.")
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--commit", action="store_true", help="copy verified files to a new formal directory")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    result = assemble(Path(args.task_root), args.commit, Path(args.output_dir) if args.output_dir else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status":"FAIL_CLOSED","error":type(exc).__name__+": "+str(exc)},ensure_ascii=False))
        raise SystemExit(2)
