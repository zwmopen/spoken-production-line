from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from save_copy_reply import (
    add_version_note, unwrap_text_block, validate_copy_body, write_copy_payload,
)


def valid_body() -> str:
    specs = [
        ("小红书1｜自然攻略版", "正文" * 330, [f"#话题{i}" for i in range(8)]),
        ("小红书2｜大纲方案版", "正文" * 355, [f"#话题{i}" for i in range(9)]),
        ("抖音｜攻略版", "正文" * 230, [f"#话题{i}" for i in range(5)]),
    ]
    parts = ["<<<COPY_FORMAT:MULTI>>>"]
    for name, content, topics in specs:
        parts.extend((f"<<<VERSION_START:{name}>>>", "标题"))
        parts.extend(["\u2800", content, "\u2800", " ".join(topics), "<<<VERSION_END>>>"])
    return "\n".join(parts)


class CopyReplyValidationTests(unittest.TestCase):
    def test_extracts_markdown_and_server_codeblock(self) -> None:
        body = valid_body()
        self.assertEqual(unwrap_text_block(f"```text\n{body}\n```"), body)
        self.assertEqual(unwrap_text_block(f'<CodeBlock language="text">{body}</CodeBlock>'), body)

    def test_validates_three_version_contract_and_counts(self) -> None:
        result = validate_copy_body(valid_body())
        self.assertEqual(result["versionCount"], 3)
        self.assertEqual(result["topicCounts"], [8, 9, 5])
        self.assertEqual(len(result["publishCharacterCounts"]), 3)

    def test_rejects_extra_douyin_topic(self) -> None:
        body = valid_body().replace("#话题4\n<<<VERSION_END>>>",
                                    "#话题4 #额外\n<<<VERSION_END>>>")
        with self.assertRaisesRegex(ValueError, "exactly 5"):
            validate_copy_body(body)

    def test_rejects_unwrapped_server_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "supported server format"):
            unwrap_text_block(valid_body())

    def test_editorial_note_is_scoped_and_idempotent(self) -> None:
        body = valid_body()
        note = "名单均需逐项核实。"
        revised = add_version_note(body, "小红书2｜大纲方案版", note)
        self.assertIn(note, revised)
        self.assertEqual(add_version_note(revised, "小红书2｜大纲方案版", note), revised)
        self.assertNotIn(note, revised.split("<<<VERSION_START:抖音｜攻略版>>>", 1)[1])
        self.assertEqual(validate_copy_body(revised)["versionCount"], 3)

    def test_copy_write_receipt_hash_matches_physical_utf8_bytes(self) -> None:
        payload = valid_body() + "\n"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "文案.txt"
            self.assertEqual(write_copy_payload(output, payload), "created")
            actual = output.read_bytes()
            self.assertEqual(actual, payload.encode("utf-8"))
            self.assertEqual(hashlib.sha256(actual).hexdigest(),
                             hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def test_existing_windows_newlines_are_idempotent_and_hashed_as_saved(self) -> None:
        payload = valid_body() + "\n"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "文案.txt"
            windows_bytes = payload.replace("\n", "\r\n").encode("utf-8")
            output.write_bytes(windows_bytes)
            self.assertEqual(write_copy_payload(output, payload), "already_same")
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),
                             hashlib.sha256(windows_bytes).hexdigest())


if __name__ == "__main__":
    unittest.main()
