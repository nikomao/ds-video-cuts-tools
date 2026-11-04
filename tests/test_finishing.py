import array
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from video_agent import media
from video_agent.finishing import normalize_visual, normalize_music, apply_visual, mix_music


class FinishingValidationTests(unittest.TestCase):
    def test_strict_settings(self):
        for config in ({"look": "unknown"}, {"filter": "arbitrary"}, {"fade_in": float("nan")},
                       {"letterbox": True}, {"fade_out": 6}):
            with self.assertRaises(ValueError):
                normalize_visual(config, 4)
        for config in ({"path": "unsafe"}, {"artifact_id": "a", "volume": 2},
                       {"artifact_id": "a", "ducking": 1}, {"artifact_id": "a", "offset": -1}):
            with self.assertRaises(ValueError):
                normalize_music(config, 4)

    def test_defaults_and_fade_bounds(self):
        self.assertEqual(normalize_visual(None, 4)["look"], "natural")
        self.assertIsNone(normalize_music(None, 4))
        value = normalize_music({"artifact_id": "music"}, .4)
        self.assertEqual(value["fade_out"], .2)
        self.assertTrue(value["ducking"])
        self.assertTrue(value["loop"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class FinishingMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "video.mp4"
        media.ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=3",
                      "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", cls.source])
        cls.music = cls.root / "music.wav"
        media.ffmpeg(["-f", "lavfi", "-i", "sine=frequency=880:duration=0.5", cls.music])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.root))

    def test_all_visual_presets_and_program_fades(self):
        for look in ("warm", "cool", "monochrome"):
            output = self.work / (look + ".mp4")
            apply_visual(self.source, output, {"look": look, "fade_in": .25, "fade_out": .25, "letterbox": .08})
            self.assertTrue(media.quality_check(output, 3)["passed"])
        # Real decoded first frame must be almost black after a program fade-in.
        first = subprocess.run([media.executable("ffmpeg"), "-v", "error", "-i", str(output),
                                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                               capture_output=True, check=True, timeout=30).stdout
        self.assertLess(sum(first) / len(first), 8)

    def test_noop_visual_and_invalid_music_offset(self):
        with self.assertRaises(ValueError):
            apply_visual(self.source, self.work / "none.mp4", {})
        with self.assertRaises(ValueError):
            mix_music(self.source, self.music, self.work / "bad.mp4", {"artifact_id": "fixture", "offset": 4})

    def test_short_music_loop_and_silent_video(self):
        output = self.work / "looped.mp4"
        mix_music(self.source, self.music, output, {"artifact_id": "fixture", "loop": True})
        self.assertTrue(media.quality_check(output, 3)["passed"])
        silent = self.work / "silent.mp4"
        media.ffmpeg(["-i", self.source, "-an", "-c:v", "copy", silent])
        mixed = self.work / "music-only.mp4"
        mix_music(silent, self.music, mixed, {"artifact_id": "fixture", "loop": False})
        info = media.quality_check(mixed, 3)
        self.assertTrue(info["passed"])
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["media"]["streams"]))

    def test_ducking_reduces_music_frequency_energy(self):
        # Compare actual decoded 880Hz music component; 440Hz source is sidechain.
        def amplitude(path):
            out = subprocess.run([media.executable("ffmpeg"), "-v", "error", "-ss", "0.8", "-i", str(path),
                                  "-t", "1", "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                                 capture_output=True, check=True, timeout=30).stdout
            samples = array.array("f")
            samples.frombytes(out)
            n = len(samples)
            re = sum(v * math.cos(2 * math.pi * 880 * i / 8000) for i, v in enumerate(samples))
            im = sum(v * math.sin(2 * math.pi * 880 * i / 8000) for i, v in enumerate(samples))
            return math.hypot(re, im) / n
        paths = []
        for duck in (False, True):
            path = self.work / (str(duck) + ".mp4")
            mix_music(self.source, self.music, path, {"artifact_id": "fixture", "volume": .8, "ducking": duck, "fade_in": 0, "fade_out": 0})
            paths.append(path)
        self.assertLess(amplitude(paths[1]), amplitude(paths[0]) * .85)

    def test_camera_motion_and_expanded_looks(self):
        # Test expanded looks (film, vintage, cyberpunk, vibrant)
        for look in ("film", "vintage", "cyberpunk", "vibrant"):
            out = self.work / f"{look}.mp4"
            apply_visual(self.source, out, {"look": look})
            qc = media.quality_check(out, 3)
            self.assertTrue(qc["passed"])

        # Test camera motions (zoom_in, zoom_out, pan_left, pan_right)
        for motion in ("zoom_in", "zoom_out", "pan_left", "pan_right"):
            out = self.work / f"motion_{motion}.mp4"
            apply_visual(self.source, out, {"motion": motion})
            qc = media.quality_check(out, 3)
            self.assertTrue(qc["passed"])

        # Test intro animations (white_fade, zoom_in)
        for intro in ("white_fade", "zoom_in"):
            out = self.work / f"intro_{intro}.mp4"
            apply_visual(self.source, out, {"intro_animation": intro})
            qc = media.quality_check(out, 3)
            self.assertTrue(qc["passed"])


if __name__ == "__main__":
    unittest.main()
