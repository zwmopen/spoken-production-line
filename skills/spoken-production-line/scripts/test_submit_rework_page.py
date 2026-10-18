import unittest

from submit_rework_page import wait_for_conversation


class FakePage:
    pass


class SubmitReworkPageTests(unittest.TestCase):
    def test_waits_for_exact_route_before_accepting_old_composer(self):
        states = [
            {"url": "https://chatgpt.com/c/old", "ready": "complete", "editors": [{"text": ""}]},
            {"url": "https://chatgpt.com/c/target?model=standard", "ready": "complete", "editors": [{"text": ""}]},
        ]
        slept = []

        def ui(_page):
            return states.pop(0)

        result = wait_for_conversation(
            FakePage(), "https://chatgpt.com/c/target", timeout=10, ui=ui,
            sleep=lambda seconds: slept.append(seconds),
        )

        self.assertEqual(result["url"], "https://chatgpt.com/c/target?model=standard")
        self.assertEqual(slept, [1])

    def test_fails_closed_if_route_never_matches(self):
        with self.assertRaisesRegex(TimeoutError, "Exact conversation did not become ready"):
            wait_for_conversation(
                FakePage(), "https://chatgpt.com/c/target", timeout=0,
                ui=lambda _page: {"url": "https://chatgpt.com/c/other", "ready": "complete", "editors": [{}]},
                sleep=lambda _seconds: None,
            )


if __name__ == "__main__":
    unittest.main()
