"""Offline tests for safe, page-specific production submission state."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from start_ready import (
    atomic_save_queue,
    build_page_prompt,
    prepare_submission_intent,
    select_ready_task,
)


class StartReadySafetyTests(unittest.TestCase):
    def test_page_placeholder_is_replaced_exactly(self):
        self.assertEqual(build_page_prompt("Generate P# now", 9), "Generate P9 now")

    def test_page_placeholder_requires_one_positive_integer(self):
        for page in (0, -1, True, "2"):
            with self.subTest(page=page), self.assertRaises(ValueError):
                build_page_prompt("Generate P# now", page)
        for template in ("Generate now", "Generate P# and P#"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                build_page_prompt(template, 1)

    def test_intent_is_durable_unknown_state_and_idempotent(self):
        task = {"id": "W1", "planMessageId": "plan-1"}
        prompt = "Generate P3"
        intent = prepare_submission_intent(task, 3, prompt, now="2026-10-18T00:00:00+00:00")
        self.assertEqual(task["generationStatus"], "SUBMISSION_UNKNOWN")
        self.assertEqual(task["activePage"], 3)
        self.assertTrue(intent["id"])
        self.assertTrue(intent["reconcileOnly"])
        self.assertEqual(intent["status"], "SUBMISSION_UNKNOWN")
        self.assertEqual(intent["promptSha256"], hashlib.sha256(prompt.encode()).hexdigest())
        with self.assertRaises(ValueError):
            prepare_submission_intent(task, 3, prompt)

    def test_supervisor_intent_id_is_preserved(self):
        task = {"id": "W1", "planMessageId": "plan-1"}
        intent = prepare_submission_intent(task, 2, "Generate P2", intent_id="supervisor-abc")
        self.assertEqual(intent["id"], "supervisor-abc")
        self.assertEqual(task["submissionIntent"]["id"], "supervisor-abc")
        with self.assertRaises(ValueError):
            prepare_submission_intent({"id": "W1", "planMessageId": "p"}, 1, "P1", intent_id="")

    def test_exact_work_id_selects_requested_plan_not_queue_head(self):
        first = {"id": "W1", "status": "PLAN_READY", "planMessageId": "p1"}
        second = {"id": "W2", "status": "PLAN_READY", "planMessageId": "p2"}
        queue = {"tasks": [first, second]}
        self.assertIs(select_ready_task(queue, "W2"), second)
        self.assertIs(select_ready_task(queue), first)
        self.assertIsNone(select_ready_task(queue, "W2", slots=0))

    def test_exact_work_id_never_falls_through_when_stale_or_missing(self):
        queue = {"tasks": [
            {"id": "W1", "status": "PLAN_READY", "planMessageId": "p1"},
            {"id": "W2", "status": "PLAN_READY", "planMessageId": "p2", "generationStatus": "START_SUBMITTED"},
        ]}
        with self.assertRaises(ValueError):
            select_ready_task(queue, "W2")
        with self.assertRaises(LookupError):
            select_ready_task(queue, "W3")

    def test_existing_start_state_is_never_reprepared(self):
        for task in (
            {"id": "W1", "planMessageId": "p", "generationStatus": "START_SUBMITTED"},
            {"id": "W1", "planMessageId": "p", "startMessageId": "m"},
            {"id": "W1", "planMessageId": "p", "submissionIntent": {"id": "i"}},
        ):
            with self.subTest(task=task), self.assertRaises(ValueError):
                prepare_submission_intent(task, 1, "P1")

    def test_queue_save_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            queue = {"account": "A", "tasks": [{"id": "W1"}]}
            atomic_save_queue(path, queue)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), queue)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
