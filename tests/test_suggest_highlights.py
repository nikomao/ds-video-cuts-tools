import unittest
from video_agent.analysis import suggest_highlights


class SuggestHighlightsTests(unittest.TestCase):
    def test_density_and_keyword_scoring(self):
        cues = [
            # Low density segment
            {"id": "c1", "start_ms": 0, "end_ms": 3000, "text": "你好"},
            # High density segment with highlight keyword
            {"id": "c2", "start_ms": 3500, "end_ms": 7500, "text": "为什么很多人总是忽略这个最关键的核心重点？记住这个秘诀！"},
            # Medium density normal segment
            {"id": "c3", "start_ms": 8000, "end_ms": 11000, "text": "接下来我们继续看下一个例子。"}
        ]
        res = suggest_highlights(cues, source_duration=12.0, target_duration=5.0, min_segment_seconds=2.0)
        self.assertTrue(res["review_required"])
        self.assertGreaterEqual(len(res["segments"]), 1)
        top = res["segments"][0]
        # Top segment should be around c2 (start 3.5s)
        self.assertAlmostEqual(top["sourceIn"], 3.5, places=1)
        self.assertGreaterEqual(top["score"], 7.0)
        self.assertIn("Keywords", top["reason"])

    def test_black_frame_penalty(self):
        cues = [
            {"id": "c1", "start_ms": 1000, "end_ms": 4000, "text": "这是重要的核心重点！"},
            {"id": "c2", "start_ms": 5000, "end_ms": 8000, "text": "这是正常的解说部分。"}
        ]
        # Simulate black issue during c1
        analysis = {
            "quality_issues": [
                {"type": "black", "start": 1.2, "end": 2.5}
            ]
        }
        res = suggest_highlights(cues, source_duration=10.0, target_duration=4.0,
                                min_segment_seconds=2.0, analysis=analysis)
        # Because c1 has a black frame penalty, c2 should have higher relative score
        self.assertGreaterEqual(len(res["segments"]), 1)
        chosen = res["segments"][0]
        self.assertAlmostEqual(chosen["sourceIn"], 5.0, places=1)

    def test_reject_invalid_inputs(self):
        with self.assertRaisesRegex(ValueError, "cues must be a non-empty list"):
            suggest_highlights([], 10.0)
        with self.assertRaisesRegex(ValueError, "min_segment_seconds"):
            suggest_highlights([{"id": "1", "start_ms": 0, "end_ms": 1000, "text": "a"}], 10.0, min_segment_seconds=0.1)
