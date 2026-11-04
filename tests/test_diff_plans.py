import unittest
from video_agent.editorial import diff_plans


class DiffPlansTests(unittest.TestCase):
    def test_identical_plans(self):
        plan = {
            "source_sha256": "abcd1234",
            "duration": 10.0,
            "segments": [{"sourceIn": 0.0, "sourceOut": 10.0, "reason": "full"}],
            "transition": {"type": "cut"},
            "subtitle_mode": "none"
        }
        res = diff_plans(plan, plan)
        self.assertEqual(res["duration"]["delta"], 0.0)
        self.assertEqual(len(res["coverage"]["removed_ranges"]), 0)
        self.assertEqual(len(res["coverage"]["added_ranges"]), 0)
        self.assertFalse(res["changes"]["transitions_changed"])
        self.assertIn("保持不变", res["human_summary"])

    def test_plan_with_cuts_and_transitions_changes(self):
        base_plan = {
            "source_sha256": "feedbeef",
            "duration": 20.0,
            "segments": [
                {"sourceIn": 0.0, "sourceOut": 10.0, "reason": "p1"},
                {"sourceIn": 10.0, "sourceOut": 20.0, "reason": "p2"}
            ],
            "transition": {"type": "cut"},
            "subtitle_mode": "soft",
            "track": "trk-1"
        }
        target_plan = {
            "source_sha256": "feedbeef",
            "duration": 15.0,
            "segments": [
                {"sourceIn": 0.0, "sourceOut": 10.0, "reason": "p1"},
                {"sourceIn": 15.0, "sourceOut": 20.0, "reason": "p2-cut"}
            ],
            "transition": {"type": "fade", "duration": 0.5},
            "subtitle_mode": "hard",
            "track": "trk-2"
        }
        res = diff_plans(base_plan, target_plan)
        self.assertEqual(res["duration"]["delta"], -5.0)
        self.assertEqual(len(res["coverage"]["removed_ranges"]), 1)
        self.assertEqual(res["coverage"]["removed_ranges"][0], (10.0, 15.0))
        self.assertEqual(res["coverage"]["removed_seconds"], 5.0)
        self.assertTrue(res["changes"]["transitions_changed"])
        self.assertTrue(res["changes"]["subtitles_changed"])
        self.assertIn("缩减了 5.00s", res["human_summary"])
        self.assertIn("新删减了 1 处原片内容", res["human_summary"])

    def test_reject_mismatched_source_plans(self):
        p1 = {"source_sha256": "sha-A", "duration": 10.0, "segments": []}
        p2 = {"source_sha256": "sha-B", "duration": 10.0, "segments": []}
        with self.assertRaisesRegex(ValueError, "Cannot compare plans from different media sources"):
            diff_plans(p1, p2)

    def test_reject_non_dict_inputs(self):
        with self.assertRaisesRegex(ValueError, "must be dictionaries"):
            diff_plans("not-a-dict", {})
