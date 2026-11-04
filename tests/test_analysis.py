"""Mock boundary tests plus real local FFmpeg evidence (no network/models)."""
import copy
import hashlib
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_agent import media
from video_agent.analysis import analyze_shots, suggest_edit


def shot(start, end, identifier="s1"):
    return dict(id=identifier, start=start, end=end, duration=end-start)


class AnalysisBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "input.bin"
        self.source.write_bytes(b"mock-only source")
        self.info = dict(duration=5, streams=[dict(index=0, codec_type="video", duration="5")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_numeric_options_reject_boolean_string_nonfinite_and_limits(self):
        for options in (dict(threshold=True), dict(threshold="0.3"), dict(threshold=float("nan")),
                        dict(threshold=-0.1), dict(threshold=1.1), dict(min_shot_seconds=0),
                        dict(min_shot_seconds=float("inf")), dict(max_shots=True),
                        dict(max_shots=0), dict(max_shots=1.5), dict(max_shots=1001)):
            with self.subTest(options=options), patch.object(media, "probe") as probe:
                with self.assertRaises(ValueError):
                    analyze_shots(self.source, self.root / "out", **options)
                probe.assert_not_called()

    def test_probe_invalid_duration_and_no_video(self):
        for info in (dict(duration=float("nan"), streams=[]), dict(duration=0, streams=[]),
                     dict(duration=True, streams=[]), dict(duration=3, streams=[]),
                     dict(duration=3, streams=[dict(codec_type="audio")]),
                     dict(duration=3, streams=[dict(codec_type="video", duration="nan")]),
                     dict(duration=3, streams=[dict(codec_type="video", duration=True)]),
                     dict(duration=3, streams=[None]),
                     dict(duration=3, streams=[dict(codec_type="video", disposition=None)]),
                     dict(duration=3, streams=[dict(codec_type="video", disposition=dict(attached_pic=1))])):
            with self.subTest(info=info), patch.object(media, "probe", return_value=info):
                with self.assertRaises(ValueError):
                    analyze_shots(self.source, self.root / "out")

    def test_candidate_spacing_cap_eof_quality_and_unique_midpoints(self):
        log = ("[showinfo] pts_time:0.1\n[showinfo] pts_time:1\n[showinfo] pts_time:2\n"
               "[showinfo] pts_time:3\n[showinfo] pts_time:4.9\n"
               "black_start:0 black_end:0.6 black_duration:0.6\n"
               "freeze_start: 2\nfreeze_end: 3\nfreeze_start: 4\n")
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if str(args[-1]).endswith(".jpg"):
                Path(args[-1]).write_bytes(b"\xff\xd8mock-jpeg")
                return "", ""
            return "", log

        with patch.object(media, "probe", return_value=self.info), \
             patch.object(media, "executable", return_value="ffmpeg"), \
             patch.object(media, "command", side_effect=command):
            first = analyze_shots(self.source, self.root / "out", max_shots=2)
            second = analyze_shots(self.source, self.root / "out", max_shots=2)
        self.assertEqual([(s["start"], s["end"]) for s in first["shots"]], [(0, 1), (1, 5)])
        self.assertEqual(len(first["scene_candidates"]), 5)
        self.assertTrue(any("max_shots" in text for text in first["limitations"]))
        self.assertEqual([(q["type"], q["start"], q["end"]) for q in first["quality_issues"]],
                         [("black", 0, .6), ("freeze", 2, 3), ("freeze", 4, 5)])
        self.assertTrue(set(s["frame_path"] for s in first["shots"]).isdisjoint(
                        s["frame_path"] for s in second["shots"]))
        for call in calls:
            if str(call[-1]).endswith(".jpg"):
                self.assertIn(float(call[call.index("-ss")+1]), [.5, 3])
                self.assertTrue(Path(call[-1]).is_relative_to((self.root / "out").resolve()))
        self.assertEqual(self.source.read_bytes(), b"mock-only source")
        json.dumps(first, allow_nan=False)

    def test_missing_or_invalid_frame_is_not_success(self):
        for result in (None, b"not-jpeg"):
            def command(args, **kwargs):
                if str(args[-1]).endswith(".jpg") and result is not None:
                    Path(args[-1]).write_bytes(result)
                return "", ""
            with self.subTest(result=result), patch.object(media, "probe", return_value=self.info), \
                 patch.object(media, "executable", return_value="ffmpeg"), \
                 patch.object(media, "command", side_effect=command):
                with self.assertRaises(media.MediaError):
                    analyze_shots(self.source, self.root / "out")

    def test_detector_failure_propagates(self):
        with patch.object(media, "probe", return_value=self.info), \
             patch.object(media, "executable", return_value="ffmpeg"), \
             patch.object(media, "command", side_effect=media.MediaError("filter absent")):
            with self.assertRaises(media.MediaError):
                analyze_shots(self.source, self.root / "out")


class SuggestEditTests(unittest.TestCase):
    def test_black_excluded_shortfall_reported_source_order_and_no_mutation(self):
        analysis = dict(duration=10, shots=[shot(0, 5), shot(5, 10, "s2")],
                        quality_issues=[dict(type="black", start=0, end=2),
                                        dict(type="freeze", start=2, end=5)])
        original = copy.deepcopy(analysis)
        result = suggest_edit(analysis, target_duration=30, max_segment_seconds=3)
        self.assertEqual(result["total_duration"], 8)
        self.assertEqual(result["shortfall_seconds"], 22)
        self.assertTrue(result["review_required"])
        self.assertEqual(original, analysis)
        previous = 2
        for segment in result["segments"]:
            self.assertGreaterEqual(segment["sourceIn"], previous)
            self.assertGreaterEqual(segment["sourceOut"]-segment["sourceIn"], 1)
            self.assertLessEqual(segment["sourceOut"]-segment["sourceIn"], 3)
            self.assertIn("freeze overlap", segment["reason"])
            previous = segment["sourceOut"]
        json.dumps(result, allow_nan=False)

    def test_freeze_penalty_prefers_unfrozen_material_then_source_order(self):
        result = suggest_edit(dict(shots=[shot(0, 6), shot(6, 12, "s2")],
                                   quality_issues=[dict(type="freeze", start=0, end=6)]),
                              target_duration=5)
        self.assertEqual(result["total_duration"], 5)
        self.assertEqual(result["segments"][0]["sourceIn"], 6)
        self.assertEqual(result["segments"][0]["sourceOut"], 11)

    def test_overlap_black_ranges_and_segment_constraints(self):
        result = suggest_edit(dict(shots=[shot(0, 20)], quality_issues=[
            dict(type="black", start=3, end=7), dict(type="black", start=6, end=10)]),
            target_duration=8, max_segment_seconds=3, min_segment_seconds=2)
        self.assertLessEqual(result["total_duration"], 8)
        last = 0
        for segment in result["segments"]:
            a, b = segment["sourceIn"], segment["sourceOut"]
            self.assertGreaterEqual(a, last)
            self.assertTrue(b <= 3 or a >= 10)
            self.assertGreaterEqual(b-a, 2)
            self.assertLessEqual(b-a, 3)
            last = b

    def test_reject_empty_black_only_short_and_malformed_shots(self):
        cases = [dict(shots=[]), dict(shots=[shot(0, .5)]),
                 dict(shots=[shot(0, 4)], quality_issues=[dict(type="black", start=0, end=4)]),
                 dict(shots=[shot(0, 3), shot(2, 4, "s2")]),
                 dict(shots=[shot(4, 5), shot(1, 2, "s2")]),
                 dict(shots=[shot(0, 2), shot(2, 3)]),
                 dict(shots=[dict(start=0, end=3, duration=4)]),
                 dict(shots=[dict(start=0, end=float("inf"))]),
                 dict(shots=[dict(start=False, end=3)]),
                 dict(shots=[dict(start="0", end=3)]),
                 dict(duration=2, shots=[shot(0, 3)]),
                 dict(shots=[shot(0, 3)], quality_issues=[dict(type="black", start=0, end=4)]),
                 dict(shots=[shot(0, 3)], quality_issues=[dict(type="emotion", start=0, end=1)]),
                 dict(shots=[shot(0, 3)], quality_issues="black"),
                 dict(shots=[shot(0, 3)] * 10001)]
        for case in cases:
            with self.subTest(case=str(case)[:180]), self.assertRaises(ValueError):
                suggest_edit(case)

    def test_floating_point_lengths_never_exceed_literal_limits(self):
        rng = random.Random(3)
        for _ in range(3000):
            start = rng.random()*1000
            end = start+rng.random()*50+1
            target = rng.random()*40+.1
            maximum = rng.random()*5+.1
            result = suggest_edit(dict(shots=[shot(start, end)]), target, maximum, .01)
            self.assertLessEqual(result["total_duration"], target)
            for segment in result["segments"]:
                self.assertLessEqual(segment["sourceOut"]-segment["sourceIn"], maximum)
                self.assertGreaterEqual(segment["sourceOut"]-segment["sourceIn"], .01)

    def test_reject_invalid_numeric_options(self):
        for options in (dict(target_duration=True), dict(target_duration=float("nan")),
                        dict(target_duration="30"), dict(target_duration=0),
                        dict(max_segment_seconds=0), dict(min_segment_seconds=float("inf")),
                        dict(min_segment_seconds=7), dict(target_duration=.5)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                suggest_edit(dict(shots=[shot(0, 5)]), **options)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "real FFmpeg/ffprobe unavailable")
class RealFFmpegAnalysisTests(unittest.TestCase):
    def test_different_solid_colors_detect_cuts_and_real_midpoint_jpegs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "colors.mkv"
            colors = ["black", "white", "red", "blue"]
            args = []
            for color in colors:
                args += ["-f", "lavfi", "-i", "color=c=%s:s=64x48:r=10:d=1" % color]
            args += ["-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
                     "-map", "[v]", "-c:v", "ffv1", source]
            media.ffmpeg(args)
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            report = analyze_shots(source, root / "frames", threshold=.3)
            self.assertEqual(len(report["shots"]), 4, report)
            for candidate, expected in zip(report["scene_candidates"], [1, 2, 3]):
                self.assertAlmostEqual(candidate["time"], expected, delta=.11)
            frame_paths = [Path(s["frame_path"]) for s in report["shots"]]
            self.assertEqual(len(set(frame_paths)), 4)
            # Decode real JPEGs and compare their pixels to the expected flat color.
            expected_rgb = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 0, 255)]
            for shot_data, frame, expected in zip(report["shots"], frame_paths, expected_rgb):
                self.assertTrue(frame.is_relative_to((root / "frames").resolve()))
                self.assertGreater(frame.stat().st_size, 100)
                output = root / (shot_data["id"] + ".rgb")
                media.ffmpeg(["-i", frame, "-vf", "scale=1:1", "-pix_fmt", "rgb24",
                              "-frames:v", "1", "-f", "rawvideo", output])
                pixel = output.read_bytes()
                self.assertEqual(len(pixel), 3)
                for actual, wanted in zip(pixel, expected):
                    self.assertLess(abs(actual-wanted), 12, (pixel, expected))
            self.assertTrue(any(q["type"] == "black" and q["start"] == 0 for q in report["quality_issues"]))
            self.assertTrue(any(q["type"] == "freeze" for q in report["quality_issues"]))
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            result = suggest_edit(report, target_duration=30, min_segment_seconds=.5)
            self.assertLessEqual(result["total_duration"], 3.11)
            self.assertGreater(result["shortfall_seconds"], 26)
            self.assertTrue(all(s["sourceIn"] >= .9 for s in result["segments"]))

    def test_uniform_material_still_has_one_real_frame_and_cap_preserves_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "white.mkv"
            media.ffmpeg(["-f", "lavfi", "-i", "color=c=white:s=32x32:r=10:d=1",
                          "-c:v", "ffv1", source])
            report = analyze_shots(source, root / "frames", max_shots=1)
            self.assertEqual(len(report["shots"]), 1)
            self.assertEqual(report["scene_candidates"], [])
            self.assertGreater(Path(report["shots"][0]["frame_path"]).stat().st_size, 100)
            self.assertAlmostEqual(report["shots"][0]["end"], 1, delta=.1)
            self.assertTrue(any(q["type"] == "freeze" for q in report["quality_issues"]))


if __name__ == "__main__":
    unittest.main()
