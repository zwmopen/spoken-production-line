"""Backfill explicit source-to-output mappings for an accepted product manifest.

The mobile gallery parser consumes sourceMaterialPath/rawMaterialPath and pageMap.
This tool only edits an accepted, physically complete work and never guesses a
missing source mapping.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def accepted_gate(manifest):
    acceptance = manifest.get("acceptance") or {}
    status = str(acceptance.get("status") or manifest.get("deliveryStatus") or "")
    if not status.startswith("PASS") and manifest.get("deliveryStatus") != "accepted_notified":
        raise ValueError("Manifest is not accepted; refusing compatibility backfill")
    planned = int(manifest.get("plannedPageCount") or acceptance.get("expectedPageCount") or 0)
    accepted = int(manifest.get("acceptedPageCount") or acceptance.get("acceptedPageCount") or 0)
    actual = int(manifest.get("actualPageCount") or 0)
    if not planned or accepted != planned or actual != planned:
        raise ValueError(f"Incomplete accepted set: planned={planned}, actual={actual}, accepted={accepted}")
    return planned, status


def build_mapping(manifest_path, manifest):
    product_root = manifest_path.parent
    source_root = Path(manifest.get("sourceMaterialPath") or manifest.get("rawMaterialPath") or manifest.get("sourceFolderPath") or "")
    if not str(source_root) or not source_root.is_dir():
        raise ValueError("No existing source material directory; refusing to infer it")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("Manifest has no page evidence")

    mappings = []
    seen_outputs = set()
    for page in pages:
        output_path = Path((page.get("output") or {}).get("path") or page.get("path") or "")
        if not output_path.is_absolute():
            output_path = product_root / output_path
        output_name = output_path.name
        if not output_name or output_name in seen_outputs or not output_path.is_file():
            raise ValueError(f"Missing or duplicate physical output: {output_path}")
        seen_outputs.add(output_name)
        expected_output_sha = page.get("sha256") or (page.get("output") or {}).get("sha256")
        if expected_output_sha and sha256(output_path).lower() != str(expected_output_sha).lower():
            raise ValueError(f"Output hash mismatch: {output_path}")

        source_entries = page.get("sourceFiles") or page.get("sourceImages") or []
        candidates = []
        for entry in source_entries:
            source_path = Path(entry.get("path") if isinstance(entry, dict) else str(entry))
            if not source_path.is_absolute():
                source_path = source_root / source_path
            if not source_path.is_file():
                continue
            expected_source_sha = entry.get("sha256") if isinstance(entry, dict) else None
            if expected_source_sha and sha256(source_path).lower() != str(expected_source_sha).lower():
                raise ValueError(f"Source hash mismatch: {source_path}")
            candidates.append(source_path)
        if not candidates:
            raise ValueError(f"No verifiable source file for {output_name}; refusing to infer mapping")

        primary = candidates[0]
        mapping = {
            "output": output_name,
            "sourcePath": str(primary),
            "sourceImage": primary.name,
        }
        if len(candidates) > 1:
            mapping["sourcePaths"] = [str(p) for p in candidates]
            mapping["sourceImages"] = [p.name for p in candidates]
        mappings.append(mapping)

    if len(mappings) != len(pages):
        raise ValueError("Page mapping count does not match the manifest")
    return str(source_root), mappings


def upgrade(acceptance_receipt, backup_root):
    receipt_path = Path(acceptance_receipt)
    receipt = read_json(receipt_path)
    if receipt.get("readbackVerified") is False or receipt.get("notificationReadbackVerified") is False:
        raise ValueError("Acceptance/notification readback explicitly failed")
    if receipt.get("status") not in {"accepted_notified", "accepted", "ACCEPTED"} and receipt.get("accepted") is not True:
        raise ValueError("Acceptance receipt is not terminal and accepted")

    manifest_path = Path(receipt.get("manifestPath") or "")
    if not manifest_path.is_file():
        raise ValueError("Acceptance receipt does not point to a physical manifest")
    original = manifest_path.read_bytes()
    old_sha = hashlib.sha256(original).hexdigest()
    manifest = json.loads(original.decode("utf-8-sig"))
    if receipt.get("workId") != manifest.get("workId"):
        raise ValueError("workId mismatch between receipt and manifest")
    planned, acceptance_status = accepted_gate(manifest)
    source_root, mapping = build_mapping(manifest_path, manifest)

    current_root = manifest.get("sourceMaterialPath") or manifest.get("rawMaterialPath") or manifest.get("sourceFolderPath")
    current_map = manifest.get("pageMap") if isinstance(manifest.get("pageMap"), list) else []
    if Path(str(current_root or "")) == Path(source_root) and current_map == mapping:
        return {
            "workId": manifest["workId"], "manifestPath": str(manifest_path),
            "status": "NO_OP_ALREADY_COMPATIBLE", "sourceRoot": source_root,
            "mappedPages": len(mapping), "acceptanceStatus": acceptance_status,
            "manifestSha256": old_sha,
        }

    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_path = backup_root / f"{manifest['workId']}.manifest.before-phone-gallery.json"
    if not backup_path.exists():
        backup_path.write_bytes(original)

    now = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    manifest["sourceMaterialPath"] = source_root
    manifest["rawMaterialPath"] = source_root
    manifest.setdefault("sourceFolderPath", source_root)
    manifest["pageMap"] = mapping
    manifest["metadataUpdatedAt"] = now
    history = manifest.setdefault("metadataChangeLog", [])
    history.append({
        "event": "phone_gallery_source_mapping_backfill",
        "at": now,
        "actor": "Codex",
        "reason": "Add parser-compatible source-root aliases and explicit per-output source mappings using existing accepted page evidence",
        "backupPath": str(backup_path),
        "workId": manifest["workId"],
    })
    atomic_json(manifest_path, manifest)
    saved = read_json(manifest_path)
    planned_after, status_after = accepted_gate(saved)
    if saved.get("pageMap") != mapping or planned_after != planned or len(saved["pageMap"]) != planned:
        raise ValueError("Post-write readback mismatch")
    return {
        "workId": saved["workId"], "manifestPath": str(manifest_path),
        "status": "UPDATED_AND_READBACK_VERIFIED", "sourceRoot": source_root,
        "mappedPages": len(mapping), "acceptanceStatus": status_after,
        "backupPath": str(backup_path), "oldManifestSha256": old_sha,
        "newManifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance-receipt", required=True)
    parser.add_argument("--backup-root", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args()
    result = upgrade(args.acceptance_receipt, args.backup_root)
    atomic_json(Path(args.receipt), result)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
