"""Pure timeline checks plus real FFmpeg synthetic render integration tests."""
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from video_agent import media, subtitles, transitions


def ranges(*pairs):
    return [{"sourceIn": a, "sourceOut": b} for a, b in pairs]


def zero_crossings_per_second(path, start, end):
    import struct
    import wave
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        data = handle.readframes(handle.getnframes())
    samples = struct.unpack("<%dh" % (len(data) // 2), data)
    window = samples[int(start * rate):int(end * rate)]
    crossings = sum(1 for a, b in zip(window, window[1:]) if (a < 0) != (b < 0))
    return crossings / (end - start)


class TimelineTests(unittest.TestCase):
    def test_default_cut_and_nonmutating(self):
        segments = ranges((1, 3), (5, 8))
        original = deepcopy(segments)
        result = transitions.build_timeline(segments)
        self.assertEqual(segments, original)
        self.assertEqual(result["duration"], 5)
        self.assertEqual(result["duration_frames"], 125)
        self.assertEqual(result["overlap_seconds"], 0)
        self.assertEqual(result["transition"], {"type": "cut", "duration": 0})
        self.assertEqual(result["spans"][1]["start_frame"], 50)
        json.dumps(result, allow_nan=False)

    def test_overlap_frame_grid_and_no_triple_overlap(self):
        result = transitions.build_timeline(ranges((0, 2), (3, 5), (7, 9)), {"type": "fade", "duration": .51})
        self.assertEqual(result["transition"]["duration"], .52)
        self.assertEqual(result["overlap_frames"], 13)
        self.assertEqual(result["overlap_seconds"], 1.04)
        self.assertEqual(result["duration_frames"], 124)
        self.assertEqual([s["start_frame"] for s in result["spans"]], [0, 37, 74])
        maximal = transitions.build_timeline(ranges((0, 2), (2, 4), (4, 6)), {"type": "dissolve", "duration": 1})
        self.assertEqual(maximal["spans"][0]["end_frame"], maximal["spans"][2]["start_frame"])

    def test_fractional_fps_is_exact_and_json_safe(self):
        rate = Fraction(30000, 1001)
        source = ranges((0, float(60 / rate)), (float(90 / rate), float(150 / rate)))
        result = transitions.build_timeline(source, {"type": "fade", "duration": float(10 / rate)}, str(rate))
        self.assertEqual(result["duration_frames"], 110)
        self.assertEqual(result["duration"], float(110 / rate))
        self.assertEqual(result["frame_rate"], "30000/1001")
        json.dumps(result, allow_nan=False)

    def test_exact_half_duration_survives_float_roundtrip(self):
        for rate in (Fraction(25), Fraction(30000, 1001), Fraction(24000, 1001), Fraction(60)):
            for length in (2, 6, 28, 60):
                for start in (0, 7, 91):
                    with self.subTest(rate=str(rate), length=length, start=start):
                        segments = ranges((float(start / rate), float((start + length) / rate))) * 3
                        setting = {"type": "fade", "duration": float((length // 2) / rate)}
                        timeline = transitions.build_timeline(segments, setting, rate)
                        self.assertEqual(timeline["duration_frames"], length * 2)
                        self.assertEqual(transitions.retime_cues([], timeline), [])

    def test_single_shot_warns_and_zero_duration_is_cut(self):
        result = transitions.build_timeline(ranges((0, 1)), {"type": "fade", "duration": 2})
        self.assertEqual(result["duration"], 1)
        self.assertEqual(result["overlap_frames"], 0)
        self.assertTrue(result["warnings"])
        self.assertEqual(result["transition"]["type"], "fade")
        zero = transitions.build_timeline(ranges((0, 1), (2, 3)), {"type": "fade", "duration": 0})
        self.assertEqual(zero["transition"]["type"], "cut")


    def test_per_boundary_mixed_cut_and_fade(self):
        result = transitions.build_timeline(
            ranges((0, 2), (3, 5), (7, 9)),
            transitions=[{"type": "fade", "duration": .4}, {"type": "cut", "duration": 0}])
        self.assertEqual(result["transition"], {"type": "mixed", "duration": 0})
        self.assertEqual(result["duration_frames"], 140)
        self.assertEqual(result["duration"], 5.6)
        self.assertEqual([s["start_frame"] for s in result["spans"]], [0, 40, 90])
        self.assertEqual([(t["type"], t["frames"]) for t in result["transitions"]],
                         [("fade", 10), ("cut", 0)])
        self.assertEqual(result["overlap_seconds"], .4)
        self.assertEqual(result["overlap_frames"], 0)
        self.assertEqual(result["overlap_frames_total"], 10)
        json.dumps(result, allow_nan=False)

    def test_per_boundary_all_cuts_and_all_fades(self):
        cuts = transitions.build_timeline(
            ranges((0, 2), (2, 4), (4, 6)),
            transitions=[{"type": "cut"}, {"type": "cut"}])
        self.assertEqual(cuts["transition"], {"type": "cut", "duration": 0})
        self.assertEqual(cuts["overlap_frames_total"], 0)
        same = transitions.build_timeline(
            ranges((0, 2), (2, 4), (4, 6)),
            transitions=[{"type": "fade", "duration": .4}, {"type": "fade", "duration": .4}])
        self.assertEqual(same["transition"], {"type": "fade", "duration": .4})
        self.assertEqual(same["duration_frames"], 130)


    def test_audio_ranges_shift_jcut_lcut_and_validate_bounds(self):
        timeline = transitions.build_timeline(
            ranges((0, 2), (3, 5), (7, 9)),
            transitions=[{"type": "cut", "jcut": .2}, {"type": "cut", "lcut": .2}])
        self.assertEqual(transitions.audio_ranges(timeline),
                         [(0, 50, 0), (70, 130, 45), (175, 225, 100)])
        self.assertTrue(any("J/L" in warning for warning in timeline["warnings"]))
        with self.assertRaises(ValueError):
            transitions.audio_ranges(timeline, 200)
        with self.assertRaises(ValueError):
            transitions.audio_ranges(transitions.build_timeline(
                ranges((0, 2), (0, 2)), transitions=[{"type": "cut", "jcut": .2}]))
        with self.assertRaises(ValueError):
            transitions.audio_ranges(transitions.build_timeline(
                ranges((0, 2), (3, 5)), transitions=[{"type": "cut", "jcut": 2.1}]))

    def test_audio_offset_validation(self):
        valid = ranges((0, 2), (3, 5))
        for setting in ([{"type": "fade", "duration": .4, "jcut": .2}],
                        [{"type": "cut", "jcut": -1}],
                        [{"type": "cut", "jcut": "0.2"}],
                        [{"type": "cut", "jcut": float("nan")}],
                        [{"type": "cut", "lcut": .001}],
                        [{"type": "cut", "unknown": .2}]):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                transitions.build_timeline(valid, transitions=setting)
        with self.assertRaises(ValueError):
            transitions.build_timeline(valid, {"type": "cut", "jcut": .2})

    def test_per_boundary_validation(self):
        valid = ranges((0, 2), (2, 4), (4, 6))
        for setting in (
                [{"type": "fade", "duration": .4}],
                [{"type": "fade", "duration": .4}] * 3,
                [{"type": "cut", "duration": .3}, {"type": "cut"}],
                [{"type": "custom"}, {"type": "cut"}],
                [{"type": "fade", "duration": .4}, {"type": "fade", "expr": "x"}],
                [{"type": "fade", "duration": .4}, {"type": "fade", "duration": 1.01}],
                [{"type": "fade", "duration": .4}, {"type": "fade", "duration": .001}],
                "fade", {}, [None, None, None]):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                transitions.build_timeline(valid, transitions=setting)
        with self.assertRaises(ValueError):
            transitions.build_timeline(valid, {"type": "fade", "duration": .4},
                                       transitions=[{"type": "fade", "duration": .4}] * 2)
        too_many = [{"type": "fade", "duration": .4}] * 51
        with self.assertRaises(ValueError):
            transitions.build_timeline(ranges((0, 2)) * 52, transitions=too_many)
        self.assertEqual(len(transitions.build_timeline(
            ranges((0, 2)) * 51, transitions=[{"type": "fade", "duration": .4}] * 50)["spans"]), 51)

    def test_whitelist_and_bad_numbers(self):
        valid = ranges((0, 2), (3, 5))
        for transition in [False, [], "fade", {"type": "custom"}, {"type": "fade;movie=/tmp/x"},
                           {"type": []}, {"type": "fade", "expr": "X"},
                           {"type": "cut", "duration": .4}]:
            with self.subTest(transition=transition), self.assertRaises(ValueError):
                transitions.build_timeline(valid, transition)
        for value in [True, False, float("nan"), float("inf"), -1, "0.4", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                transitions.build_timeline(valid, {"type": "fade", "duration": value})
            with self.subTest(source=value), self.assertRaises(ValueError):
                transitions.build_timeline(ranges((value, 3)))
        for rate in [True, False, "nan", "inf", "0/0", 0, 241, None, {}]:
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                transitions.build_timeline(valid, fps=rate)

    def test_reject_oversized_and_collapsed_and_transition_limit(self):
        for segments, duration in [(ranges((0, 1), (2, 4)), .51),
                                   (ranges((0, 1.08), (2, 4)), .54),
                                   (ranges((0, 2), (2, 4)), .001)]:
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                transitions.build_timeline(segments, {"type": "fade", "duration": duration})
        with self.assertRaises(ValueError):
            transitions.build_timeline(ranges((0, .001)))
        with self.assertRaises(ValueError):
            transitions.build_timeline([])
        with self.assertRaises(ValueError):
            transitions.build_timeline(ranges((0, 2)) * 52, {"type": "fade", "duration": .4})
        self.assertEqual(len(transitions.build_timeline(ranges((0, 2)) * 51, {"type": "fade", "duration": .4})["spans"]), 51)
        self.assertEqual(len(transitions.build_timeline(ranges((0, 2)) * 500)["spans"]), 500)


class RetimeTests(unittest.TestCase):
    def test_midpoint_ownership_words_metadata_and_no_mutation(self):
        cues = [{"id": "first", "start_ms": 0, "end_ms": 2000, "text": "first sentence", "speaker": "A",
                 "custom": {"retain": True}, "words": [{"start_ms": 1400, "end_ms": 2000, "text": "sentence"}]},
                {"id": "second", "start_ms": 2000, "end_ms": 4000, "text": "second sentence",
                 "words": [{"start_ms": 2000, "end_ms": 2500, "text": "second"}]}]
        original = deepcopy(cues)
        timeline = transitions.build_timeline(ranges((0, 2), (2, 4)), {"type": "fade", "duration": .4})
        result = transitions.retime_cues(cues, timeline)
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in result], [(0, 1800), (1800, 3600)])
        self.assertEqual(result[0]["words"][0]["end_ms"], 1800)
        self.assertEqual(result[1]["words"][0]["start_ms"], 1800)
        self.assertEqual(result[0]["custom"], {"retain": True})
        self.assertEqual(result[0]["source_cue_id"], "first")
        self.assertEqual(result[1]["segment_index"], 1)
        self.assertEqual(cues, original)
        self.assertFalse([x for x in subtitles.qc_cues(result, 3600) if x["severity"] == "error"])
        self.assertTrue(subtitles.serialize_subtitles(result, "srt"))

    def test_repeated_reordered_segments_and_collision_safe_ids(self):
        cues = [{"id": "a", "start_ms": 0, "end_ms": 1000, "text": "a"},
                {"id": "a~2", "start_ms": 1000, "end_ms": 2000, "text": "b"}]
        timeline = transitions.build_timeline(ranges((0, 2), (0, 2), (0, 2)), {"type": "dissolve", "duration": .4})
        result = transitions.retime_cues(cues, timeline)
        self.assertEqual(len(result), 6)
        self.assertEqual(len({cue["id"] for cue in result}), 6)
        self.assertEqual(result[2]["id"], "a~3")
        for first, second in zip(result, result[1:]):
            self.assertLessEqual(first["end_ms"], second["start_ms"])

    def test_fractional_grid_odd_overlap_and_final_bounds(self):
        rate = Fraction(30000, 1001)
        segments = ranges((0, float(60 / rate)), (float(75 / rate), float(135 / rate)))
        timeline = transitions.build_timeline(segments, {"type": "dissolve", "duration": float(11 / rate)}, rate)
        cues = [{"id": "all", "start_ms": 0, "end_ms": 10000, "text": "everything"}]
        result = transitions.retime_cues(cues, timeline)
        self.assertEqual(result[0]["end_ms"], result[1]["start_ms"])
        self.assertLessEqual(result[-1]["end_ms"], timeline["duration"] * 1000)


    def test_per_boundary_midpoint_and_hard_cut_ownership(self):
        cues = [{"id": "all", "start_ms": 0, "end_ms": 6000, "text": "everything"}]
        timeline = transitions.build_timeline(
            ranges((0, 2), (2, 4), (4, 6)),
            transitions=[{"type": "fade", "duration": .4}, {"type": "cut", "duration": 0}])
        result = transitions.retime_cues(cues, timeline)
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in result],
                         [(0, 1800), (1800, 3600), (3600, 5600)])
        for first, second in zip(result, result[1:]):
            self.assertLessEqual(first["end_ms"], second["start_ms"])
        timeline["transitions"][0]["frames"] = 11
        with self.assertRaises(ValueError):
            transitions.retime_cues(cues, timeline)

    def test_legacy_timeline_without_transitions_list_still_maps(self):
        cues = [{"id": "a", "start_ms": 0, "end_ms": 4000, "text": "text"}]
        full = transitions.build_timeline(ranges((0, 2), (2, 4)), {"type": "fade", "duration": .4})
        legacy = {key: value for key, value in full.items()
                  if key not in ("transitions", "overlap_frames_total")}
        mapped = transitions.retime_cues(cues, legacy)
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in mapped],
                         [(0, 1800), (1800, 3600)])


    def test_audio_offsets_do_not_move_subtitles(self):
        cues = [{"id": "all", "start_ms": 0, "end_ms": 4000, "text": "everything"}]
        timeline = transitions.build_timeline(
            ranges((0, 2), (3, 5)), transitions=[{"type": "cut", "jcut": .2, "lcut": .2}])
        mapped = transitions.retime_cues(cues, timeline)
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in mapped],
                         [(0, 2000), (2000, 3000)])
        timeline["transitions"][0]["jcut_frames"] = 6
        with self.assertRaises(ValueError):
            transitions.retime_cues(cues, timeline)

    def test_cut_compatibility_and_tamper_rejection(self):
        cues = [{"id": "a", "start_ms": 0, "end_ms": 2000, "text": "text"}]
        segments = ranges((.4, 1.2), (0, .8))
        result = transitions.retime_cues(cues, transitions.build_timeline(segments))
        expected = subtitles.retime_cues(cues, segments)
        self.assertEqual([{k: v for k, v in c.items() if k not in ("source_cue_id", "segment_index")} for c in result], expected)
        timeline = transitions.build_timeline(segments)
        timeline["spans"][1]["outputIn"] += 1
        with self.assertRaises(ValueError):
            transitions.retime_cues(cues, timeline)


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            media.executable("ffmpeg")
            media.executable("ffprobe")
        except media.MediaError as exc:
            raise unittest.SkipTest(str(exc))
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "sound.mp4"
        cls.silent = cls.root / "silent.mp4"
        cls.fractional = cls.root / "fractional.mp4"
        media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x96:r=25:d=6", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.source])
        media.ffmpeg(["-i", cls.source, "-map", "0:v:0", "-c:v", "copy", "-an", cls.silent])
        media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x96:r=30000/1001:d=6", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                      "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.fractional])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def assert_render_qc(self, output, timeline, audio=True):
        info = media.probe(output)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual(int(video["nb_frames"]), timeline["duration_frames"])
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertEqual(Fraction(video["avg_frame_rate"]), Fraction(timeline["frame_rate"]))
        self.assertAlmostEqual(float(video["duration"]), timeline["duration"], delta=.0011)
        self.assertAlmostEqual(info["duration"], timeline["duration"], delta=.002)
        self.assertEqual(any(s["codec_type"] == "audio" for s in info["streams"]), audio)
        if audio:
            stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
            self.assertAlmostEqual(float(stream["duration"]), timeline["duration"], delta=.002)
        media.command([media.executable("ffmpeg"), "-v", "error", "-xerror", "-i", output, "-f", "null", "-"])

    def test_real_fade_dissolve_and_silent_sources(self):
        segments = ranges((0, 2), (2, 4), (4, 6))
        for kind in ("fade", "dissolve"):
            for silent in (False, True):
                with self.subTest(kind=kind, silent=silent):
                    source = self.silent if silent else self.source
                    setting = {"type": kind, "duration": .4}
                    timeline = transitions.build_timeline(segments, setting)
                    output = self.root / f"{kind}-{silent}.mp4"
                    self.assertEqual(transitions.render_sequence(source, segments, output, transition=setting), output.resolve())
                    self.assert_render_qc(output, timeline, not silent)
                    cues = [{"id": "all", "start_ms": 0, "end_ms": 6000, "text": "cross all transitions"}]
                    mapped = transitions.retime_cues(cues, timeline)
                    self.assertEqual(len(mapped), 3)
                    self.assertFalse([x for x in subtitles.qc_cues(mapped, timeline["duration"] * 1000) if x["severity"] == "error"])

    def test_remaining_whitelisted_filters_and_maximal_overlap(self):
        segments = ranges((0, 2), (2, 4), (4, 6))
        for kind in ("fadeblack", "wipeleft", "wiperight", "slideleft", "slideright"):
            with self.subTest(kind=kind):
                setting = {"type": kind, "duration": 1}
                timeline = transitions.build_timeline(segments, setting)
                output = self.root / f"whitelist-{kind}.mp4"
                transitions.render_sequence(self.source, segments, output, transition=setting)
                self.assert_render_qc(output, timeline)

    def test_fractional_fps_real_duration(self):
        info = media.probe(self.fractional)
        segments = media.normalize_segments(ranges((0, 2), (2.5, 4.5)), info)
        setting = {"type": "fadeblack", "duration": .4}
        timeline = transitions.build_timeline(segments, setting, media.frame_rate(info))
        output = self.root / "fractional-out.mp4"
        transitions.render_sequence(self.fractional, segments, output, transition=setting)
        self.assert_render_qc(output, timeline)

    def test_fractional_fps_mixed_real_duration(self):
        info = media.probe(self.fractional)
        segments = media.normalize_segments(ranges((0, 2), (2.5, 4.5), (4.5, 6)), info)
        setting = [{"type": "fadeblack", "duration": .4}, {"type": "cut", "duration": 0}]
        timeline = transitions.build_timeline(segments, fps=media.frame_rate(info),
                                              transitions=setting)
        output = self.root / "fractional-mixed.mp4"
        transitions.render_sequence(self.fractional, segments, output, transitions=setting)
        self.assert_render_qc(output, timeline)

    def test_cut_and_single_shot_real_duration(self):
        for name, segments, setting in [("cut", ranges((0, 1), (2, 3)), None),
                                        ("single", ranges((1, 3)), {"type": "fade", "duration": .4})]:
            with self.subTest(name=name):
                output = self.root / f"{name}.mp4"
                transitions.render_sequence(self.source, segments, output, transition=setting)
                self.assert_render_qc(output, transitions.build_timeline(segments, setting))


    def test_real_mixed_cut_and_fade(self):
        segments = ranges((0, 2), (2, 4), (4, 6))
        setting = [{"type": "fade", "duration": .4}, {"type": "cut", "duration": 0}]
        timeline = transitions.build_timeline(segments, transitions=setting)
        output = self.root / "mixed-cut-fade.mp4"
        self.assertEqual(transitions.render_sequence(
            self.source, segments, output, transitions=setting), output.resolve())
        self.assert_render_qc(output, timeline)
        cues = [{"id": "all", "start_ms": 0, "end_ms": 6000, "text": "all"}]
        mapped = transitions.retime_cues(cues, timeline)
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in mapped],
                         [(0, 1800), (1800, 3600), (3600, 5600)])
        # Exercise xfade -> concat -> xfade chaining, not just one ordering.
        reversed_setting = [{"type": "cut", "duration": 0}, {"type": "fade", "duration": .4}]
        reversed_timeline = transitions.build_timeline(segments, transitions=reversed_setting)
        reversed_output = self.root / "mixed-cut-then-fade.mp4"
        transitions.render_sequence(self.source, segments, reversed_output,
                                    transitions=reversed_setting)
        self.assert_render_qc(reversed_output, reversed_timeline)
        alternating = [{"type": "fade", "duration": .3}, {"type": "cut", "duration": 0},
                       {"type": "fade", "duration": .3}]
        four = ranges((0, 1.5), (1.5, 3), (3, 4.5), (4.5, 6))
        alternating_timeline = transitions.build_timeline(four, transitions=alternating)
        alternating_output = self.root / "mixed-alternating.mp4"
        transitions.render_sequence(self.source, four, alternating_output,
                                    transitions=alternating)
        self.assert_render_qc(alternating_output, alternating_timeline)
        silent_output = self.root / "mixed-silent.mp4"
        transitions.render_sequence(self.silent, segments, silent_output,
                                    transitions=setting)
        self.assert_render_qc(silent_output, timeline, audio=False)

    def test_real_mixed_cut_keeps_hard_color_boundary(self):
        source = self.root / "three-colors.mp4"
        media.ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=64x48:r=25:d=2",
                      "-f", "lavfi", "-i", "color=c=green:s=64x48:r=25:d=2",
                      "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=25:d=2",
                      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
                      "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
                      "-map", "[v]", "-map", "3:a:0", "-c:v", "libx264",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "6", source])
        segments = ranges((0, 2), (2, 4), (4, 6))
        setting = [{"type": "fade", "duration": .4}, {"type": "cut", "duration": 0}]
        output = self.root / "three-colors-out.mp4"
        transitions.render_sequence(source, segments, output, transitions=setting)
        self.assert_render_qc(output, transitions.build_timeline(segments, transitions=setting))
        for seconds, expected in ((3.5, (0, 128, 0)), (3.7, (0, 0, 255))):
            frame = self.root / ("mixed-%s.rgb" % seconds)
            media.ffmpeg(["-ss", str(seconds), "-i", output, "-vf", "scale=1:1",
                          "-pix_fmt", "rgb24", "-frames:v", "1", "-f", "rawvideo", frame])
            pixel = frame.read_bytes()
            self.assertEqual(len(pixel), 3)
            for actual, wanted in zip(pixel, expected):
                self.assertLess(abs(actual - wanted), 24, (seconds, pixel, expected))


    def test_real_jcut_audio_leads_picture(self):
        source = self.root / "jcut-source.mp4"
        media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x96:r=25:d=6",
                      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
                      "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=1.5",
                      "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=3",
                      "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1,aresample=48000[a]",
                      "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-t", "6", source])
        segments = ranges((0, 2), (4, 6))
        setting = [{"type": "cut", "jcut": .5}]
        timeline = transitions.build_timeline(segments, transitions=setting)
        self.assertGreater(timeline["transitions"][0]["jcut_frames"], 0)
        output = self.root / "jcut-out.mp4"
        transitions.render_sequence(source, segments, output, transitions=setting)
        self.assert_render_qc(output, timeline)
        wav = self.root / "jcut.wav"
        media.ffmpeg(["-i", output, "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", wav])
        # 440Hz gives ~880 zero crossings/s; 880Hz gives ~1760.
        self.assertGreater(zero_crossings_per_second(wav, 1.6, 1.9), 1400)
        self.assertGreater(zero_crossings_per_second(wav, 2.1, 2.5), 1400)
        self.assertLess(zero_crossings_per_second(wav, 0.2, 0.8), 1100)

    def test_real_lcut_audio_lingers_after_picture_cut(self):
        source = self.root / "lcut-source.mp4"
        media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=160x96:r=25:d=6",
                      "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.5",
                      "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2",
                      "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1.5",
                      "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1,aresample=48000[a]",
                      "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-t", "6", source])
        segments = ranges((0, 2), (4, 6))
        setting = [{"type": "cut", "lcut": .5}]
        timeline = transitions.build_timeline(segments, transitions=setting)
        self.assertGreater(timeline["transitions"][0]["lcut_frames"], 0)
        output = self.root / "lcut-out.mp4"
        transitions.render_sequence(source, segments, output, transitions=setting)
        self.assert_render_qc(output, timeline)
        wav = self.root / "lcut.wav"
        media.ffmpeg(["-i", output, "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", wav])
        self.assertGreater(zero_crossings_per_second(wav, 2.1, 2.4), 700)
        self.assertLess(zero_crossings_per_second(wav, 2.1, 2.4), 1100)
        self.assertGreater(zero_crossings_per_second(wav, 2.6, 3.2), 1400)

    def test_aspect_whitelist_no_overwrite_and_invalid_filter(self):
        output = self.root / "square.mp4"
        transitions.render_sequence(self.silent, ranges((0, 1), (2, 3)), output, aspect="1:1", transition={"type": "wipeleft", "duration": .4})
        info = media.probe(output)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (720, 720))
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            transitions.render_sequence(self.source, ranges((0, 1)), output)
        self.assertEqual(output.read_bytes(), before)
        with patch.object(media, "ffmpeg") as invoke:
            with self.assertRaises(ValueError):
                transitions.render_sequence(self.source, ranges((0, 1), (2, 3)), self.root / "bad.mp4", transition={"type": "fade;null", "duration": .4})
            with self.assertRaises(ValueError):
                transitions.render_sequence(self.source, ranges((0, 1)), self.root / "bad.mp4", aspect="scale=1:1")
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
