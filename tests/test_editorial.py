import unittest
from video_agent.editorial import review_plan


class EditorialTests(unittest.TestCase):
    def plan(self, **kw):
        return {"segments": [{"sourceIn": .2, "sourceOut": 3, "reason": "hook"}], "duration": 2.8, **kw}

    def test_word_cut_has_traceable_evidence(self):
        cues = [{"id": "a", "start_ms": 0, "end_ms": 500, "text": "hello", "words": [
            {"start_ms": 100, "end_ms": 400, "text": "hello"}]}]
        value = review_plan(self.plan(), cues)
        item = next(i for i in value["issues"] if i["code"] == "cut_inside_word")
        self.assertEqual(item["evidence"]["cue_id"], "a")
        self.assertTrue(value["review_required"])

    def test_without_words_only_claim_cue(self):
        value = review_plan(self.plan(), [{"id": "a", "start_ms": 0, "end_ms": 500, "text": "hello"}])
        self.assertIn("cut_inside_cue", [i["code"] for i in value["issues"]])
        self.assertNotIn("cut_inside_word", [i["code"] for i in value["issues"]])

    def test_legacy_fractional_fps_plan_uses_frame_exact_subtitles(self):
        from video_agent.engine import timeline_for_plan
        from video_agent.transitions import retime_cues
        from fractions import Fraction
        for fps, a, b in (("24", 1, 128), ("24", 2, 127), ("30000/1001", 2, 127)):
            rate = float(Fraction(fps))
            segment = {"sourceIn": a / rate, "sourceOut": b / rate}
            duration = (b - a) * 500 / rate
            plan = {"segments": [segment] * 500, "duration": duration}
            info = {"duration": 6, "streams": [{"codec_type": "video", "avg_frame_rate": fps, "duration": "6"}]}
            timeline = timeline_for_plan(plan, info)
            cues = [{"id": "a", "start_ms": 0, "end_ms": 6000, "text": "Example"}]
            output = retime_cues(cues, timeline)
            self.assertLessEqual(abs(output[-1]["end_ms"] - duration * 1000), 1)

    def test_unquantized_legacy_approval_fails_closed(self):
        from video_agent.engine import timeline_for_plan
        plan = {"segments": [{"sourceIn": 0, "sourceOut": .5}] * 30, "duration": 15}
        info = {"duration": 3, "streams": [{"codec_type": "video", "avg_frame_rate": "25", "duration": "3"}]}
        with self.assertRaisesRegex(ValueError, "Approved duration"):
            timeline_for_plan(plan, info)

    def test_per_boundary_mixed_transitions_warn_only_for_non_cut(self):
        plan = self.plan(segments=[{"sourceIn": 0, "sourceOut": 2}, {"sourceIn": 2, "sourceOut": 4},
                                   {"sourceIn": 4, "sourceOut": 6}],
                         transitions=[{"type": "fade", "duration": .4}, {"type": "cut", "duration": 0}])
        value = review_plan(plan, [{"id": "a", "start_ms": 0, "end_ms": 100, "text": "a"}])
        codes = {i["code"] for i in value["issues"]}
        self.assertIn("dialogue_overlap", codes)
        self.assertNotIn("stylized_transition", codes)
        jcut = self.plan(segments=[{"sourceIn": 0, "sourceOut": 2}, {"sourceIn": 2, "sourceOut": 4}],
                         transitions=[{"type": "cut", "jcut": .2, "jcut_frames": 5}])
        self.assertIn("audio_offset_overlap", [i["code"] for i in review_plan(jcut)["issues"]])

    def test_transition_and_black_warning(self):
        plan = self.plan(segments=[{"sourceIn": 0, "sourceOut": 2}, {"sourceIn": 1, "sourceOut": 2}],
                         transition={"type": "wipeleft"}, music={"ducking": True}, visual={"letterbox": .1})
        value = review_plan(plan, [{"id": "a", "start_ms": 0, "end_ms": 100, "text": "a"}],
                            {"quality_issues": [{"type": "black", "start": 0, "end": .5}]})
        codes = {i["code"] for i in value["issues"]}
        self.assertTrue({"source_quality_warning", "repeated_source", "dialogue_overlap", "stylized_transition", "energy_not_speech", "letterbox_occlusion"} <= codes)


if __name__ == "__main__":
    unittest.main()
