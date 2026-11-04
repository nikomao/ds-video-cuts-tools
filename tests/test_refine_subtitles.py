import unittest
from video_agent.subtitles import refine_subtitles, validate_cues


class RefineSubtitlesTests(unittest.TestCase):
    def test_short_cues_remain_intact(self):
        cues = [
            {"id": "c1", "start_ms": 100, "end_ms": 1000, "text": "你好世界"}
        ]
        res = refine_subtitles(cues, max_chars=10)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["text"], "你好世界")
        self.assertEqual(res[0]["id"], "c1")

    def test_split_long_cue_with_words(self):
        cues = [
            {
                "id": "c-long",
                "start_ms": 0,
                "end_ms": 5000,
                "text": "欢迎大家收看本期视频，今天我们要分享一个非常实用的技巧！",
                "words": [
                    {"start_ms": 0, "end_ms": 1500, "text": "欢迎大家收看本期视频，"},
                    {"start_ms": 1600, "end_ms": 3200, "text": "今天我们要分享"},
                    {"start_ms": 3300, "end_ms": 4900, "text": "一个非常实用的技巧！"}
                ]
            }
        ]
        res = refine_subtitles(cues, max_chars=12)
        self.assertGreaterEqual(len(res), 2)
        # Verify valid structure and non-overlapping times
        validate_cues(res)
        self.assertTrue(all(len(c["text"]) <= 20 for c in res))
        self.assertEqual(res[0]["id"], "c-long_1")
        self.assertEqual(res[1]["id"], "c-long_2")

    def test_split_long_cue_without_words_interpolation(self):
        long_text = "这是一段非常长没有词级时间戳的中文对白内容，我们需要把它拆开成短句以便排版。"
        cues = [
            {"id": "text_only", "start_ms": 1000, "end_ms": 6000, "text": long_text}
        ]
        res = refine_subtitles(cues, max_chars=15)
        self.assertGreaterEqual(len(res), 2)
        validate_cues(res)
        # Check start and end bounds
        self.assertEqual(res[0]["start_ms"], 1000)
        self.assertEqual(res[-1]["end_ms"], 6000)
        # Strictly monotonic
        for i in range(len(res) - 1):
            self.assertLessEqual(res[i]["end_ms"], res[i+1]["start_ms"])

    def test_parameter_validations(self):
        with self.assertRaisesRegex(ValueError, "max_chars must be an integer between 6 and 200"):
            refine_subtitles([{"id": "1", "start_ms": 0, "end_ms": 1000, "text": "a"}], max_chars=3)
        with self.assertRaisesRegex(ValueError, "split_punctuations must be a string"):
            refine_subtitles([{"id": "1", "start_ms": 0, "end_ms": 1000, "text": "a"}], split_punctuations=123)
