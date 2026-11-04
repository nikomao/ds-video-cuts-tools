import unittest
from video_agent.analysis import clean_speech


class CleanSpeechTests(unittest.TestCase):
    def test_clean_speech_with_word_timestamps(self):
        cues = [
            {
                "id": "cue-1",
                "start_ms": 1000,
                "end_ms": 4000,
                "text": "嗯 大家好",
                "words": [
                    {"start_ms": 1000, "end_ms": 1400, "text": "嗯", "probability": 0.95},
                    {"start_ms": 1800, "end_ms": 2800, "text": "大家", "probability": 0.98},
                    {"start_ms": 2900, "end_ms": 3800, "text": "好", "probability": 0.99},
                ]
            },
            {
                "id": "cue-2",
                "start_ms": 7000,
                "end_ms": 9000,
                "text": "今天分享剪辑",
                "words": [
                    {"start_ms": 7100, "end_ms": 7900, "text": "今天", "probability": 0.97},
                    {"start_ms": 8000, "end_ms": 8400, "text": "分享", "probability": 0.96},
                    {"start_ms": 8450, "end_ms": 8900, "text": "剪辑", "probability": 0.98},
                ]
            }
        ]
        result = clean_speech(cues, source_duration=10.0, max_pause_seconds=0.6, padding_seconds=0.08)
        self.assertTrue(result["review_required"])
        self.assertEqual(result["fillers_count"], 1)
        self.assertEqual(result["fillers_detected"][0]["normalized"], "嗯")
        self.assertEqual(result["fillers_detected"][0]["cue_id"], "cue-1")
        # Ensure segments are produced and don't include the filler at 1.0-1.4s
        segments = result["segments"]
        self.assertGreaterEqual(len(segments), 1)
        # Check that the first segment starts around word 2 (1.8s - 0.08 padding = 1.72s)
        self.assertGreaterEqual(segments[0]["sourceIn"], 1.70)
        self.assertLessEqual(segments[-1]["sourceOut"], 10.0)

    def test_clean_speech_english_fillers(self):
        cues = [
            {
                "id": "cue-en",
                "start_ms": 0,
                "end_ms": 3000,
                "text": "Um welcome to the show",
                "words": [
                    {"start_ms": 100, "end_ms": 400, "text": "Um"},
                    {"start_ms": 800, "end_ms": 1400, "text": "welcome"},
                    {"start_ms": 1500, "end_ms": 1800, "text": "to"},
                    {"start_ms": 1850, "end_ms": 2000, "text": "the"},
                    {"start_ms": 2100, "end_ms": 2700, "text": "show"},
                ]
            }
        ]
        result = clean_speech(cues, source_duration=4.0)
        self.assertEqual(result["fillers_count"], 1)
        self.assertEqual(result["fillers_detected"][0]["normalized"], "um")

    def test_custom_fillers(self):
        cues = [
            {"id": "1", "start_ms": 500, "end_ms": 1500, "text": "foo bar", "words": [
                {"start_ms": 500, "end_ms": 900, "text": "foo"},
                {"start_ms": 1000, "end_ms": 1500, "text": "bar"}
            ]}
        ]
        result = clean_speech(cues, source_duration=2.0, fillers=["foo"])
        self.assertEqual(result["fillers_count"], 1)
        self.assertEqual(result["fillers_detected"][0]["text"], "foo")

    def test_without_words_cue_level_fallback(self):
        cues = [
            {"id": "c1", "start_ms": 500, "end_ms": 1200, "text": "嗯"},
            {"id": "c2", "start_ms": 2000, "end_ms": 4000, "text": "正式内容"}
        ]
        result = clean_speech(cues, source_duration=5.0)
        self.assertEqual(result["fillers_count"], 1)
        self.assertEqual(result["fillers_detected"][0]["cue_id"], "c1")
        self.assertEqual(len(result["segments"]), 1)
        self.assertGreaterEqual(result["segments"][0]["sourceIn"], 1.9)

    def test_reject_invalid_parameters(self):
        with self.assertRaisesRegex(ValueError, "cues must be a non-empty list"):
            clean_speech([], 10.0)
        with self.assertRaisesRegex(ValueError, "source_duration"):
            clean_speech([{"id": "1", "start_ms": 0, "end_ms": 1000, "text": "hi"}], -5)
        with self.assertRaisesRegex(ValueError, "max_pause_seconds is outside"):
            clean_speech([{"id": "1", "start_ms": 0, "end_ms": 1000, "text": "hi"}], 5.0, max_pause_seconds=0.01)
        with self.assertRaisesRegex(ValueError, "padding_seconds is outside"):
            clean_speech([{"id": "1", "start_ms": 0, "end_ms": 1000, "text": "hi"}], 5.0, padding_seconds=-1.0)
