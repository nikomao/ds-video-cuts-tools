import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from video_agent.store import Project, write_json
from video_agent.engine import submit, run_job
from video_agent import media


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "input.bin"
        self.source.write_bytes(b"not-media: store tests only")
        self.p = Project.create(self.root / "project", self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_import_never_changes_original(self):
        self.assertEqual(self.p.source.read_bytes(), self.source.read_bytes())
        self.assertNotEqual(self.p.source, self.source)
        with self.assertRaises(ValueError):
            Project.create(self.p.root, self.source)

    def test_source_integrity(self):
        self.p.source.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            _ = self.p.source

    def test_path_escape_and_unknown_artifact(self):
        with self.assertRaises(ValueError):
            self.p.resolve("../input.bin")
        with self.assertRaises(ValueError):
            self.p.artifact("unknown")

    def test_exclusive_claim_retry(self):
        job = self.p.enqueue("probe", {})
        first = self.p.claim(job["id"])
        self.assertEqual(first["attempts"], 1)
        with self.assertRaises(ValueError):
            self.p.claim(job["id"])
        self.p.finish(job["id"], "failed", error="test")
        self.assertEqual(self.p.claim(job["id"], retry=True)["attempts"], 2)

    def test_failed_attempt_can_retry(self):
        with patch("video_agent.engine.execute", side_effect=ValueError("failed")):
            with self.assertRaises(ValueError):
                submit(self.p, "probe", {})
        job = self.p.jobs()[0]
        self.assertEqual(job["status"], "failed")
        with patch("video_agent.engine.execute", return_value=([], {"ok": True})):
            result = run_job(self.p, job["id"], retry=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue((self.p.root / "jobs" / job["id"] / "attempt-2" / "result.json").exists())

    def test_success_cache_and_tamper(self):
        path = self.p.root / "result.json"
        write_json(path, {"ok": True})
        with patch("video_agent.engine.execute", return_value=([path], {})) as execute:
            first = submit(self.p, "probe", {})
            second = submit(self.p, "probe", {})
            self.assertEqual(execute.call_count, 1)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["cached"])
        path.write_text("tampered")
        with self.assertRaises(ValueError):
            submit(self.p, "probe", {})

    def test_initialization_failure_records_failed(self):
        job = self.p.enqueue("probe", {})
        with patch("video_agent.engine.Path.mkdir", side_effect=PermissionError("test init failure")):
            with self.assertRaises(PermissionError):
                run_job(self.p, job["id"])
        self.assertEqual(self.p.job(job["id"])["status"], "failed")

    def test_model_environment_and_content_invalidate_cache(self):
        with patch("video_agent.engine.execute", return_value=([], {})):
            with patch.dict("os.environ", {"VIDEO_AGENT_MODEL": "model-A"}):
                a = submit(self.p, "probe", {})
            with patch.dict("os.environ", {"VIDEO_AGENT_MODEL": "model-B"}):
                b = submit(self.p, "probe", {})
        self.assertNotEqual(a["job_id"], b["job_id"])
        self.assertEqual(self.p.context(a["job_id"])["VIDEO_AGENT_MODEL"], "model-A")
        self.assertNotIn("VIDEO_AGENT_API_KEY", self.p.context(a["job_id"]))

    def test_transitive_manifest_integrity(self):
        audio = self.p.root / "voice.wav"
        audio.write_bytes(b"audio")
        aid = self.p.register("fixture", audio)["id"]
        manifest = self.p.root / "manifest.json"
        write_json(manifest, {"clips": [{"artifact_id": aid}]})
        mid = self.p.register("fixture", manifest)["id"]
        self.p.verify_dependencies(mid)
        audio.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            self.p.verify_dependencies(mid)

    def test_changed_context_cannot_silently_retry(self):
        with patch("video_agent.engine.execute", side_effect=ValueError("fixture")):
            with patch.dict("os.environ", {"VIDEO_AGENT_MODEL": "model-A"}):
                with self.assertRaises(ValueError):
                    submit(self.p, "probe", {})
        job = self.p.jobs()[0]
        with patch.dict("os.environ", {"VIDEO_AGENT_MODEL": "model-B"}):
            with self.assertRaisesRegex(ValueError, "configuration"):
                run_job(self.p, job["id"], retry=True)
        self.assertEqual(self.p.job(job["id"])["status"], "failed")

    def test_cancelled_execution_records_state(self):
        with patch("video_agent.engine.execute", side_effect=KeyboardInterrupt("test")):
            with self.assertRaises(KeyboardInterrupt):
                submit(self.p, "probe", {})
        self.assertEqual(self.p.jobs()[0]["status"], "cancelled")

    def test_parameter_whitelist(self):
        with self.assertRaises(ValueError):
            submit(self.p, "probe", {"shell": "echo unsafe"})
        with self.assertRaises(ValueError):
            submit(self.p, "transcribe", {"allow_download": "false"})


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "synthetic source ' ;.mp4"
        media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=3",
                      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.source])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.root))
        self.p = Project.create(self.work / "project", self.source)

    def track(self):
        result = submit(self.p, "import_subtitles", {"cues": [
            {"id": "a", "start_ms": 200, "end_ms": 1100, "text": "Hello"},
            {"id": "b", "start_ms": 1600, "end_ms": 2600, "text": "World"}]})
        return result["artifacts"][0]["id"]

    def plan(self, **kw):
        return submit(self.p, "plan", kw)["artifacts"][0]["id"]

    def test_real_probe_audio_proxy(self):
        result = submit(self.p, "probe", {})
        self.assertAlmostEqual(result["detail"]["duration"], 3, places=1)
        audio = submit(self.p, "extract_audio", {})
        self.assertEqual(audio["detail"]["streams"][0]["sample_rate"], "16000")
        proxy = submit(self.p, "proxy", {})
        self.assertEqual(proxy["detail"]["streams"][0]["width"], 640)

    def test_subtitle_exports_and_harness_translation(self):
        track = self.track()
        translated = submit(self.p, "apply_translations", {"track": track, "language": "zh", "translations": {"a": "你好", "b": "世界"}})
        tid = translated["artifacts"][0]["id"]
        for fmt in ("srt", "vtt", "ass"):
            result = submit(self.p, "export_subtitles", {"track": tid, "format": fmt})
            self.assertIn("你好", self.p.artifact(result["artifacts"][0]["id"]).read_text())
        with self.assertRaises(ValueError):
            submit(self.p, "apply_translations", {"track": track, "translations": {"a": "incomplete"}})

    def test_plan_approval_render_soft_and_retiming(self):
        plan = self.plan(segments=[{"sourceIn": 0.5, "sourceOut": 1.3}, {"sourceIn": 1.8, "sourceOut": 2.8}],
                         subtitle_mode="soft", track=self.track())
        with self.assertRaises(ValueError):
            submit(self.p, "render", {"plan": plan})
        self.p.approve(plan)
        job = self.p.jobs()[0]
        render = run_job(self.p, job["id"], retry=True)
        self.assertTrue(render["detail"]["qc"]["passed"])
        video = next(a for a in render["artifacts"] if a["path"].endswith(".mp4"))
        info = media.probe(self.p.artifact(video["id"]))
        self.assertAlmostEqual(info["duration"], 1.8, delta=.15)
        self.assertTrue(any(s["codec_type"] == "subtitle" for s in info["streams"]))
        track = next(a for a in render["artifacts"] if a["path"].endswith("edited-track.json"))
        cues = json.loads(self.p.artifact(track["id"]).read_text())["cues"]
        self.assertEqual(cues[0]["start_ms"], 0)
        self.assertEqual(cues[1]["start_ms"], 800)

    def test_real_preview(self):
        plan = self.plan(track=self.track(), subtitle_mode="soft")
        result = submit(self.p, "preview", {"plan": plan})
        self.assertTrue(result["detail"]["preview"])
        self.assertTrue(result["detail"]["qc"]["passed"])
        self.assertTrue(any(s["codec_type"] == "subtitle" for s in result["detail"]["qc"]["media"]["streams"]))

    def test_plan_rejects_bad_segments(self):
        for segments in ([], [{"sourceIn": -1, "sourceOut": 1}], [{"sourceIn": 1, "sourceOut": 4}], [{"sourceIn": True, "sourceOut": 2}]):
            with self.assertRaises(ValueError):
                self.plan(segments=segments)

    def test_mutated_plan_invalidates_approval(self):
        plan = self.plan()
        self.p.approve(plan)
        self.p.artifact(plan).write_text("{}")
        with self.assertRaises(ValueError):
            self.p.require_approval(plan)

    def test_hard_subtitles_capability_gate(self):
        plan = self.plan(track=self.track(), subtitle_mode="hard")
        self.p.approve(plan)
        if media.doctor()["hard_subtitles"]:
            result = submit(self.p, "render", {"plan": plan})
            self.assertTrue(result["detail"]["qc"]["passed"])
        else:
            with self.assertRaisesRegex(media.MediaError, "libass"):
                submit(self.p, "render", {"plan": plan})

    def test_real_mix_from_registered_voice(self):
        # Sine wave is explicitly synthetic test audio, not a claimed TTS inference.
        audio = self.p.root / "synthetic-voice.wav"
        media.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=880:duration=1", audio])
        artifact = self.p.register("fixture", audio)
        manifest = self.p.root / "fixture-manifest.json"
        write_json(manifest, {"source_sha256": self.p.manifest["source_sha256"], "clips": [
            {"id": "voice1", "start_ms": 500, "end_ms": 1500, "artifact_id": artifact["id"]}]})
        mid = self.p.register("fixture", manifest)["id"]
        plan = self.plan(voice_manifest=mid)
        self.p.approve(plan)
        result = submit(self.p, "render", {"plan": plan})
        self.assertTrue(result["detail"]["qc"]["passed"])

    def test_many_subframe_segments_use_same_timeline(self):
        plan = self.plan(segments=[{"sourceIn": 0, "sourceOut": .5}] * 30)
        data = json.loads(self.p.artifact(plan).read_text())
        self.assertTrue(data["timeline"]["cuts_snapped_to_frames"])
        self.p.approve(plan)
        result = submit(self.p, "render", {"plan": plan})
        self.assertTrue(result["detail"]["qc"]["passed"])
        self.assertAlmostEqual(result["detail"]["qc"]["media"]["duration"], data["duration"], delta=.08)

    def test_output_timeline_track_cannot_be_remapped_twice(self):
        plan = self.plan(track=self.track())
        result = submit(self.p, "preview", {"plan": plan})
        edited = next(a["id"] for a in result["artifacts"] if a["path"].endswith("edited-track.json"))
        with self.assertRaisesRegex(ValueError, "Output-timeline"):
            self.plan(track=edited)

    def test_wrong_voice_track_timing_rejected(self):
        path = self.p.root / "bad-manifest.json"
        write_json(path, {"source_sha256": self.p.manifest["source_sha256"], "clips": [
            {"id": "a", "start_ms": 700, "end_ms": 1300}]})
        mid = self.p.register("fixture", path)["id"]
        with self.assertRaisesRegex(ValueError, "timing"):
            self.plan(track=self.track(), voice_manifest=mid)

    def test_tts_manifest_integration_and_transitive_cache(self):
        # Mock only synthesis; exercise real engine registration and dependency checks.
        track = self.track()
        def fixture(cues, work, **kw):
            path = work / "voice.wav"
            media.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=880:duration=1", path])
            return {"provider": "fixture", "voice": "synthetic", "manifest": "unused", "cues": [
                {**cues[0], "wav_path": str(path), "duration_ms": 1000}]}
        with patch("video_agent.providers.synthesize", side_effect=fixture):
            result = submit(self.p, "tts", {"track": track, "provider": "say", "confirmed": True})
        manifest = json.loads(self.p.artifact(result["artifacts"][0]["id"]).read_text())
        self.assertEqual(len(manifest["clips"]), 1)
        clip = self.p.artifact(manifest["clips"][0]["artifact_id"])
        clip.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            submit(self.p, "tts", {"track": track, "provider": "say", "confirmed": True})

    def test_silence_detection_real(self):
        result = submit(self.p, "plan", {"remove_silence": True})
        self.assertAlmostEqual(result["detail"]["duration"], 3, places=1)

    def test_no_audio_video_render(self):
        source = self.work / "silent.mp4"
        media.ffmpeg(["-i", self.source, "-an", "-c:v", "copy", source])
        project = Project.create(self.work / "silent-project", source)
        plan = submit(project, "plan", {})["artifacts"][0]["id"]
        project.approve(plan)
        self.assertTrue(submit(project, "render", {"plan": plan})["detail"]["qc"]["passed"])


if __name__ == "__main__":
    unittest.main()
