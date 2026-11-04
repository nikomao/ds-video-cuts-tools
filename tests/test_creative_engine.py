import json
from pathlib import Path
import shutil
import tempfile
import unittest
from video_agent import media
from video_agent.engine import submit
from video_agent.store import Project


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CreativeEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "scenes.mp4"
        media.ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=2",
                      "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=2",
                      "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                      "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a:0",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "4", cls.source])
        cls.music = cls.root / "licensed-fixture.wav"
        media.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=880:duration=1", cls.music])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.root))
        self.project = Project.create(self.work / "project", self.source)

    def artifact(self, result, suffix):
        return next(a for a in result["artifacts"] if a["path"].endswith(suffix))

    def analysis(self):
        result = submit(self.project, "analyze_shots", {})
        return self.artifact(result, "shot-analysis.json")["id"], result

    def music_artifact(self):
        result = submit(self.project, "import_music", {"file": str(self.music), "rights_confirmed": True, "license_note": "Synthetic test tone created by test suite"})
        return self.artifact(result, ".wav")["id"]

    def track(self):
        result = submit(self.project, "import_subtitles", {"cues": [
            {"id": "first", "start_ms": 0, "end_ms": 1900, "text": "Example data: red"},
            {"id": "second", "start_ms": 2100, "end_ms": 3900, "text": "Example data: blue"}]})
        return self.artifact(result, "track.json")["id"]

    def test_analysis_frames_and_suggestions_are_registered(self):
        aid, result = self.analysis()
        report = result["detail"]
        self.assertEqual(len(report["shots"]), 2)
        self.assertGreaterEqual(len(result["artifacts"]), 3)
        for shot in report["shots"]:
            self.project.artifact(shot["frame_artifact_id"])
        draft = submit(self.project, "suggest_edit", {"analysis": aid, "target_duration": 3, "max_segment_seconds": 2})
        self.assertTrue(draft["detail"]["review_required"])
        self.assertLessEqual(draft["detail"]["total_duration"], 3)
        self.assertTrue(submit(self.project, "analyze_shots", {})["cached"])
        self.project.artifact(report["shots"][0]["frame_artifact_id"]).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            submit(self.project, "suggest_edit", {"analysis": aid})

    def test_rights_confirmation_and_audio_only_gate(self):
        with self.assertRaises(ValueError):
            submit(self.project, "import_music", {"file": str(self.music)})
        with self.assertRaises(ValueError):
            submit(self.project, "import_music", {"file": str(self.source), "rights_confirmed": True})

    def test_unlicensed_audio_artifact_not_accepted(self):
        audio = submit(self.project, "extract_audio", {})["artifacts"][0]["id"]
        with self.assertRaisesRegex(ValueError, "rights-confirmed"):
            submit(self.project, "plan", {"music": {"artifact_id": audio}})

    def test_crossfade_music_visual_subtitles_approval_qc(self):
        aid, _ = self.analysis()
        plan = submit(self.project, "plan", {
            "segments": [{"sourceIn": 0, "sourceOut": 2, "reason": "red opening"}, {"sourceIn": 2, "sourceOut": 4, "reason": "blue conclusion"}],
            "analysis": aid, "track": self.track(), "subtitle_mode": "soft",
            "transition": {"type": "dissolve", "duration": .4},
            "visual": {"look": "warm", "fade_in": .2, "fade_out": .3},
            "music": {"artifact_id": self.music_artifact(), "volume": .1, "ducking": True}})
        pid = self.artifact(plan, "edit-plan.json")["id"]
        self.assertAlmostEqual(plan["detail"]["duration"], 3.6)
        review = submit(self.project, "review_plan", {"plan": pid})
        self.assertTrue(review["detail"]["review_required"])
        self.assertIn("dialogue_overlap", [i["code"] for i in review["detail"]["issues"]])
        preview = submit(self.project, "preview", {"plan": pid})
        self.assertTrue(preview["detail"]["qc"]["passed"])
        self.project.approve(pid)  # Test fixture only.
        rendered = submit(self.project, "render", {"plan": pid})
        self.assertTrue(rendered["detail"]["qc"]["passed"])
        self.assertAlmostEqual(rendered["detail"]["qc"]["media"]["duration"], 3.6, delta=.06)
        cues = json.loads(self.project.artifact(self.artifact(rendered, "edited-track.json")["id"]).read_text())["cues"]
        self.assertLessEqual(cues[0]["end_ms"], cues[1]["start_ms"])
        self.assertEqual(cues[1]["start_ms"], 1800)
        self.assertTrue(submit(self.project, "render", {"plan": pid})["cached"])
        music_file = self.project.artifact(plan["detail"]["music"]["artifact_id"])
        music_file.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            submit(self.project, "render", {"plan": pid})

    def test_new_effects_have_new_approval(self):
        a = submit(self.project, "plan", {})["artifacts"][0]["id"]
        self.project.approve(a)
        b = submit(self.project, "plan", {"visual": {"look": "cool"}})["artifacts"][0]["id"]
        with self.assertRaises(ValueError):
            self.project.require_approval(b)

    def test_rights_record_tamper_blocks_plan_cache_preview_and_render(self):
        music = self.music_artifact()
        params = {"music": {"artifact_id": music}}
        result = submit(self.project, "plan", params)
        plan = self.artifact(result, "edit-plan.json")["id"]
        self.project.approve(plan)
        self.project.artifact(result["detail"]["music_license"]).write_text("{}")
        for task, arguments in (("plan", params), ("preview", {"plan": plan}), ("render", {"plan": plan})):
            with self.assertRaises(ValueError):
                submit(self.project, task, arguments)

    def test_legacy_music_plan_without_rights_binding_rejected(self):
        from video_agent.store import write_json
        result = submit(self.project, "plan", {"music": {"artifact_id": self.music_artifact()}})
        data = dict(result["detail"])
        data.pop("music_license")
        path = self.project.root / "legacy-music-plan.json"
        write_json(path, data)
        plan = self.project.register("fixture", path)["id"]
        self.project.approve(plan)
        with self.assertRaisesRegex(ValueError, "Legacy music plan"):
            submit(self.project, "render", {"plan": plan})

    def test_original_no_music_plan_remains_renderable(self):
        from video_agent.store import write_json
        data = dict(submit(self.project, "plan", {})["detail"])
        for key in ("transition", "visual", "music", "music_license", "analysis"):
            data.pop(key, None)
        data["timeline"] = {"frame_rate": "25", "cuts_snapped_to_frames": True}
        path = self.project.root / "original-plan.json"
        write_json(path, data)
        plan = self.project.register("fixture", path)["id"]
        self.project.approve(plan)
        rendered = submit(self.project, "render", {"plan": plan})
        self.assertTrue(rendered["detail"]["qc"]["passed"])

    def test_per_boundary_transitions_plan_review_render(self):
        plan = submit(self.project, "plan", {
            "segments": [{"sourceIn": 0, "sourceOut": 1, "reason": "red first"},
                         {"sourceIn": 1, "sourceOut": 2, "reason": "red second"},
                         {"sourceIn": 2, "sourceOut": 4, "reason": "blue"}],
            "transitions": [{"type": "fade", "duration": .2}, {"type": "cut", "duration": 0}],
            "track": self.track(), "subtitle_mode": "soft"})
        detail = plan["detail"]
        self.assertEqual([t["type"] for t in detail["transitions"]], ["fade", "cut"])
        self.assertAlmostEqual(detail["duration"], 3.8)
        pid = self.artifact(plan, "edit-plan.json")["id"]
        review = submit(self.project, "review_plan", {"plan": pid})
        self.assertTrue(review["detail"]["review_required"])
        preview = submit(self.project, "preview", {"plan": pid})
        self.assertTrue(preview["detail"]["qc"]["passed"])
        self.project.approve(pid)
        rendered = submit(self.project, "render", {"plan": pid})
        self.assertTrue(rendered["detail"]["qc"]["passed"])
        self.assertAlmostEqual(rendered["detail"]["qc"]["media"]["duration"], 3.8, delta=.06)

    def test_transition_and_transitions_are_exclusive(self):
        with self.assertRaises(ValueError):
            submit(self.project, "plan", {"transition": {"type": "cut", "duration": 0},
                                          "transitions": []})


    def test_jcut_plan_preview_and_source_bounds(self):
        plan = submit(self.project, "plan", {
            "segments": [{"sourceIn": 0, "sourceOut": 1, "reason": "red"},
                         {"sourceIn": 2, "sourceOut": 4, "reason": "blue"}],
            "transitions": [{"type": "cut", "jcut": .2}]})
        detail = plan["detail"]
        self.assertEqual(detail["transitions"][0]["jcut_frames"], 5)
        pid = self.artifact(plan, "edit-plan.json")["id"]
        preview = submit(self.project, "preview", {"plan": pid})
        self.assertTrue(preview["detail"]["qc"]["passed"])
        for segments, setting in (
                ([{"sourceIn": 0, "sourceOut": 1}, {"sourceIn": 0, "sourceOut": 1}],
                 [{"type": "cut", "jcut": .5}]),
                ([{"sourceIn": 2, "sourceOut": 4}, {"sourceIn": 2, "sourceOut": 4}],
                 [{"type": "cut", "lcut": .5}])):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                submit(self.project, "plan", {"segments": segments, "transitions": setting})

    def test_nested_filter_injection_and_invalid_transition_rejected(self):
        for params in ({"transition": {"type": "fade;movie=unsafe", "duration": .4}},
                       {"visual": {"filter": "movie=unsafe"}}, {"music": {"file": "unsafe"}}):
            with self.assertRaises(ValueError):
                submit(self.project, "plan", params)

    def test_clean_speech_and_diff_plans_integration(self):
        # 1. Test clean_speech task
        track_id = self.track()
        clean_res = submit(self.project, "clean_speech", {"track": track_id})
        self.assertEqual(clean_res["status"], "completed")
        clean_artifact = self.artifact(clean_res, "speech-clean-suggestions.json")
        data = json.loads(self.project.artifact(clean_artifact["id"]).read_text())
        self.assertTrue(data["review_required"])
        self.assertIn("segments", data)

        # 2. Test diff_plans task
        plan_a = submit(self.project, "plan", {"segments": [{"sourceIn": 0, "sourceOut": 2, "reason": "a"}]})
        plan_b = submit(self.project, "plan", {"segments": [{"sourceIn": 0, "sourceOut": 1, "reason": "b"},
                                                            {"sourceIn": 2, "sourceOut": 3, "reason": "c"}]})
        pid_a = self.artifact(plan_a, "edit-plan.json")["id"]
        pid_b = self.artifact(plan_b, "edit-plan.json")["id"]
        diff_res = submit(self.project, "diff_plans", {"base_plan": pid_a, "target_plan": pid_b})
        self.assertEqual(diff_res["status"], "completed")
        diff_artifact = self.artifact(diff_res, "plan-diff.json")
        diff_data = json.loads(self.project.artifact(diff_artifact["id"]).read_text())
        self.assertEqual(diff_data["source_sha256"], self.project.manifest["source_sha256"])
        self.assertIn("成片时长", diff_data["human_summary"])

    def test_suggest_highlights_and_refine_subtitles_integration(self):
        track_id = self.track()
        # Test refine_subtitles task
        refine_res = submit(self.project, "refine_subtitles", {"track": track_id, "max_chars": 10})
        self.assertEqual(refine_res["status"], "completed")
        refined_art = self.artifact(refine_res, "refined-track.json")
        refined_data = json.loads(self.project.artifact(refined_art["id"]).read_text())
        self.assertIn("cues", refined_data)

        # Test suggest_highlights task
        hl_res = submit(self.project, "suggest_highlights", {"track": track_id, "target_duration": 2.0})
        self.assertEqual(hl_res["status"], "completed")
        hl_art = self.artifact(hl_res, "highlight-suggestions.json")
        hl_data = json.loads(self.project.artifact(hl_art["id"]).read_text())
        self.assertTrue(hl_data["review_required"])
        self.assertIn("segments", hl_data)


if __name__ == "__main__":
    unittest.main()
