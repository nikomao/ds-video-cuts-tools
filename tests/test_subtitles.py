"""Standard-library subtitle boundary, editing, security and round-trip tests."""

from copy import deepcopy
import random
import subprocess
import unittest

from video_agent import media
from video_agent.subtitles import (
    apply_translations, parse_subtitles, qc_cues, retime_cues,
    serialize_subtitles, validate_cues,
)


def cue(identity="a", start=0, end=1000, text="Hello", **metadata):
    return dict(id=identity, start_ms=start, end_ms=end, text=text, **metadata)


class ValidationTests(unittest.TestCase):
    def test_normalizes_and_deep_copies(self):
        source = [cue(1, 0.1, 1000.2, "a\r\nb", confidence=0.9,
                      words=[dict(start_ms=0.1, end_ms=1000, text="a")],
                      extra={"tags": ["original"]})]
        normalized = validate_cues(source)
        self.assertEqual(normalized[0]["id"], "1")
        self.assertEqual(normalized[0]["start_ms"], 0)
        self.assertEqual(normalized[0]["end_ms"], 1000)
        self.assertEqual(normalized[0]["text"], "a\nb")
        normalized[0]["extra"]["tags"].append("new")
        normalized[0]["words"][0]["text"] = "changed"
        self.assertEqual(source[0]["extra"]["tags"], ["original"])
        self.assertEqual(source[0]["words"][0]["text"], "a")

    def test_empty_touching_and_duration_boundary(self):
        self.assertEqual(validate_cues([], 0), [])
        source = [cue(), cue("b", 1000, 2000)]
        self.assertEqual(validate_cues(source, 2000), source)
        with self.assertRaises(ValueError):
            validate_cues(source, 1999)

    def test_nonfinite_negative_boolean_and_nonnumeric_times(self):
        for field in ("start_ms", "end_ms"):
            for value in (float("nan"), float("inf"), -float("inf"), -1, -0.01, True, None, "1000", []):
                with self.subTest(field=field, value=value):
                    value_cue = cue()
                    value_cue[field] = value
                    with self.assertRaises(ValueError):
                        validate_cues([value_cue])

    def test_duration_validation_even_when_empty(self):
        for value in (-1, float("nan"), float("inf"), True, "1000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_cues([], value)

    def test_fractional_overlap_and_overflow_not_hidden_by_rounding(self):
        values = [cue(end=1000.4), cue("b", 1000.3, 2000)]
        with self.assertRaises(ValueError):
            validate_cues(values)
        self.assertIn("overlap", {issue["code"] for issue in qc_cues(values)})
        with self.assertRaises(ValueError):
            validate_cues([cue(end=1000.4)], 1000)
        with self.assertRaises(ValueError):
            validate_cues([cue(start=0.4, words=[dict(start_ms=0.3, end_ms=500, text="x")])])

    def test_bad_structure(self):
        for values in (None, {}, "cues", [None], [{}], [dict(id="a", start_ms=0, end_ms=1)]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_cues(values)

    def test_bad_ids_and_text(self):
        for identity in (None, True, [], "", "  ", "x\ny", "x\x00y", "x-->y"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                validate_cues([cue(identity)])
        for text in (None, [], "x\x00y"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_cues([cue(text=text)])

    def test_duplicate_order_overlap_and_collapsed_intervals(self):
        bad = [
            [cue(1), cue("1", 1000, 2000)],
            [cue("a", 1000, 2000), cue("b", 0, 500)],
            [cue(), cue("b", 999, 2000)],
            [cue(end=0)], [cue(start=10, end=9)],
            [cue(start=0.1, end=0.2)],
        ]
        for values in bad:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_cues(values)

    def test_optional_metadata_validation(self):
        for metadata in (dict(speaker=12), dict(confidence=1.1),
                         dict(confidence=-0.1), dict(confidence=True),
                         dict(confidence=float("nan")), dict(words="word"),
                         dict(words=[{}]),
                         dict(words=[dict(start_ms=0, end_ms=1001, text="x")]),
                         dict(words=[dict(start_ms=1, end_ms=1, text="x")]),
                         dict(words=[dict(start_ms=0, end_ms=500, text="a"),
                                     dict(start_ms=400, end_ms=600, text="b")])):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                validate_cues([cue(**metadata)])


class FormatTests(unittest.TestCase):
    def assert_roundtrip(self, values, fmt):
        result = parse_subtitles(serialize_subtitles(values, fmt), fmt)
        if fmt.lower() == "srt":
            self.assertEqual([item["id"] for item in result],
                             [str(index) for index in range(1, len(values) + 1)])
            self.assertEqual([{k: v for k, v in item.items() if k != "id"} for item in result],
                             [{k: v for k, v in item.items() if k != "id"} for item in values])
        else:
            self.assertEqual(result, values)

    def test_srt_exports_numeric_sequence_without_mutating_ids(self):
        values = [cue("a"), cue("b", 1000, 2000), cue("007", 2000, 3000)]
        snapshot = deepcopy(values)
        encoded = serialize_subtitles(values, "srt")
        self.assertEqual([block.splitlines()[0] for block in encoded.strip().split("\n\n")], ["1", "2", "3"])
        self.assertEqual(values, snapshot)
        self.assert_roundtrip(values, "srt")

    def test_srt_numeric_sequences_accepted_by_real_ffmpeg(self):
        try:
            ffmpeg = media.executable("ffmpeg")
        except media.MediaError:
            self.skipTest("FFmpeg not available")
        encoded = serialize_subtitles([cue("a"), cue("b", 1000, 2000)], "srt")
        result = subprocess.run([ffmpeg, "-v", "error", "-f", "srt",
                                 "-i", "pipe:0", "-map", "0:s:0", "-c:s", "copy",
                                 "-f", "srt", "pipe:1"],
                                input=encoded.encode("utf-8"), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        decoded = parse_subtitles(result.stdout.decode("utf-8"), "srt")
        self.assertEqual([item["text"] for item in decoded], ["Hello", "Hello"])

    def test_all_formats_roundtrip(self):
        values = [cue("01", 0, 1000, "你好, world\nsecond line"),
                  cue("b / %,", 1000, 3661230, "  preserved spaces  ")]
        for fmt in ("srt", "vtt", "ass", "SRT"):
            with self.subTest(format=fmt):
                encoded = serialize_subtitles(values, fmt)
                self.assert_roundtrip(values, fmt)

    def test_empty_document_and_empty_cue_roundtrip(self):
        for fmt in ("srt", "vtt", "ass"):
            with self.subTest(format=fmt):
                self.assertEqual(parse_subtitles(serialize_subtitles([], fmt), fmt), [])
                values = [cue(text=""), cue("b", 1000, 2000, ""), cue("c", 2000, 3000)]
                self.assert_roundtrip(values, fmt)

    def test_srt_bom_crlf_missing_id_and_multiline(self):
        text = "\ufeff00:00:00,000 --> 00:00:01,234\r\nHello\r\nWorld\r\n\r\nsecond\r\n00:00:01,234 --> 00:00:02,000\r\nBye"
        self.assertEqual(parse_subtitles(text, "srt"), [cue("1", 0, 1234, "Hello\nWorld"), cue("second", 1234, 2000, "Bye")])

    def test_vtt_header_settings_metadata_short_timestamp(self):
        text = ("WEBVTT subtitles\nLanguage: en\n\nNOTE ignored\ncomment\n\n"
                "STYLE\n::cue {font-size: 20px;}\n\nREGION\nid:r\n\n"
                "00:00.001 --> 00:01.234 align:start position:10%\nHello\n")
        self.assertEqual(parse_subtitles(text, "vtt"), [cue("1", 1, 1234)])
        self.assertEqual(parse_subtitles("WEBVTT", "vtt"), [])

    def test_native_ass_format_speaker_tags_commas_and_comments(self):
        text = ("[Script Info]\nTitle: Example\n[Events]\n"
                "Format: Start, End, Name, Text\n"
                "Comment: 0:00:00.00,0:00:01.00,,ignored\n"
                "Dialogue: 0:00:00.10,0:00:01.25,Bob,{\\b1}Hi{\\b0}, there\\Nnext\\hword\n")
        self.assertEqual(parse_subtitles(text, "ass"), [cue("1", 100, 1250, "Hi, there\nnext word", speaker="Bob")])

    def test_ass_safe_text_and_metadata_roundtrip(self):
        text = "{\\pos(0,0)}literal \\N \\n \\h \\\\ {brace}\nnormal"
        values = [cue("a;cue-text:evil%,", text=text, speaker="Bob,\nInjected")]
        encoded = serialize_subtitles(values, "ass")
        dialogue = next(line for line in encoded.splitlines() if line.startswith("Dialogue:"))
        rendered_text = dialogue.split(",", 9)[-1]
        self.assertNotIn("{", rendered_text)
        self.assertNotIn("}", rendered_text)
        self.assertNotIn("\\pos", rendered_text)
        self.assertEqual(rendered_text.count("\\N"), 1)
        self.assertEqual(parse_subtitles(encoded, "ass"), values)
        self.assertEqual(sum(line.startswith("Dialogue:") for line in encoded.splitlines()), 1)

    def test_ass_precision_rounding_and_collapse(self):
        result = parse_subtitles(serialize_subtitles([cue(start=13, end=1007)], "ass"), "ass")
        self.assertEqual((result[0]["start_ms"], result[0]["end_ms"]), (10, 1010))
        with self.assertRaises(ValueError):
            serialize_subtitles([cue(start=1, end=4)], "ass")

    def test_text_block_ambiguity_rejected(self):
        for fmt in ("srt", "vtt"):
            for text in ("a\n\nb", "\ntext", "text\n", "a\n \nb"):
                with self.subTest(format=fmt, text=text), self.assertRaises(ValueError):
                    serialize_subtitles([cue(text=text)], fmt)
        with self.assertRaises(ValueError):
            serialize_subtitles([cue("NOTE comment")], "vtt")

    def test_invalid_formats_and_input_types(self):
        for fmt in ("ssa", "txt", "", None, 1):
            with self.subTest(format=fmt):
                with self.assertRaises(ValueError):
                    parse_subtitles("", fmt)
                with self.assertRaises(ValueError):
                    serialize_subtitles([], fmt)
        with self.assertRaises(ValueError):
            parse_subtitles(None, "srt")

    def test_malformed_documents(self):
        cases = [
            ("bad", "srt"),
            ("1\n00:60:00,000 --> 01:00:00,000\nx", "srt"),
            ("1\n00:00:00,000 --> 00:00:00,000\nx", "srt"),
            ("1\n00:00:00,000 --> 00:00:01,000 garbage\nx", "srt"),
            ("00:00.000 --> 00:01.000\nx", "vtt"),
            ("WEBVTT\n00:00.000 --> 00:01.000\nx", "vtt"),
            ("[Script Info]\nTitle: no events", "ass"),
            ("[Events]\nFormat: Start, End\n", "ass"),
            ("[Events]\nFormat: Start, End, Start, Text\n", "ass"),
            ("[Events]\nDialogue: bad", "ass"),
            ("[Events]\nFormat: Start, End, Text\nDialogue: 0:99:00.00,1:00:00.00,x", "ass"),
        ]
        for text, fmt in cases:
            with self.subTest(format=fmt, text=text), self.assertRaises(ValueError):
                parse_subtitles(text, fmt)

    def test_parse_rejects_duplicate_ids_and_overlaps(self):
        for fmt in ("srt", "vtt"):
            text = "x\n00:00:00.000 --> 00:00:02.000\na\n\nx\n00:00:01.000 --> 00:00:03.000\nb"
            if fmt == "vtt":
                text = "WEBVTT\n\n" + text
            with self.subTest(format=fmt), self.assertRaises(ValueError):
                parse_subtitles(text, fmt)

    def test_seeded_roundtrip_many_boundaries(self):
        rng = random.Random(420)
        values, end = [], 0
        for index in range(100):
            start = end + rng.randrange(0, 20) * 10
            end = start + rng.randrange(1, 500) * 10
            text = rng.choice(["Plain 中文", "two\nlines", "comma, literal", "{tag} \\N", ""])
            values.append(cue(str(index), start, end, text))
        for fmt in ("srt", "vtt", "ass"):
            with self.subTest(format=fmt):
                self.assert_roundtrip(values, fmt)


class RetimeTests(unittest.TestCase):
    def test_clip_cross_cut_and_preserve_gaps(self):
        values = [cue("a", 500, 2500), cue("b", 3000, 4500), cue("c", 5000, 6000)]
        result = retime_cues(values, [dict(sourceIn=1, sourceOut=2), dict(sourceIn=3.5, sourceOut=5.5)])
        self.assertEqual(result, [cue("a", 0, 1000), cue("b", 1000, 2000), cue("c", 2500, 3000)])

    def test_reorder_repeat_and_unique_collision_safe_ids(self):
        values = [cue("a", 0, 1000), cue("a~2", 1000, 2000)]
        result = retime_cues(values, [dict(sourceIn=0, sourceOut=2), dict(sourceIn=0, sourceOut=1)])
        self.assertEqual([x["id"] for x in result], ["a", "a~2", "a~3"])
        self.assertEqual([(x["start_ms"], x["end_ms"]) for x in result], [(0, 1000), (1000, 2000), (2000, 3000)])
        reverse = retime_cues(values, [dict(sourceIn=1, sourceOut=2), dict(sourceIn=0, sourceOut=1)])
        self.assertEqual([x["id"] for x in reverse], ["a~2", "a"])

    def test_cue_spanning_multiple_segments_splits(self):
        result = retime_cues([cue(end=5000)], [dict(sourceIn=0, sourceOut=1), dict(sourceIn=3, sourceOut=4)])
        self.assertEqual(result, [cue("a", 0, 1000), cue("a~2", 1000, 2000)])

    def test_boundary_touch_excludes_empty_intersections(self):
        result = retime_cues([cue(), cue("b", 1000, 2000), cue("c", 2000, 3000)], [dict(sourceIn=1, sourceOut=2)])
        self.assertEqual(result, [cue("b", 0, 1000)])

    def test_words_clip_and_map_and_input_not_mutated(self):
        values = [cue("a", 1000, 4000, words=[dict(start_ms=1000, end_ms=2000, text="one"),
                                                dict(start_ms=2000, end_ms=3000, text="two"),
                                                dict(start_ms=3000, end_ms=4000, text="three")])]
        snapshot = deepcopy(values)
        result = retime_cues(values, [dict(sourceIn=1.5, sourceOut=2.5), dict(sourceIn=3.5, sourceOut=4)])
        self.assertEqual(result[0]["words"], [dict(start_ms=0, end_ms=500, text="one"), dict(start_ms=500, end_ms=1000, text="two")])
        self.assertEqual(result[1]["words"], [dict(start_ms=1000, end_ms=1500, text="three")])
        self.assertEqual(values, snapshot)

    def test_empty_ranges_and_leading_gap(self):
        self.assertEqual(retime_cues([cue()], []), [])
        self.assertEqual(retime_cues([], [dict(sourceIn=0, sourceOut=1)]), [])
        self.assertEqual(retime_cues([cue()], [dict(sourceIn=5, sourceOut=6)]), [])
        self.assertEqual(retime_cues([cue()], [dict(sourceIn=5, sourceOut=6), dict(sourceIn=0, sourceOut=1)]), [cue(start=1000, end=2000)])

    def test_invalid_segments(self):
        for segments in (None, {}, [None], [{}], [dict(sourceIn=1, sourceOut=1)],
                         [dict(sourceIn=-1, sourceOut=1)], [dict(sourceIn=0, sourceOut=float("inf"))],
                         [dict(sourceIn=0, sourceOut=float("nan"))], [dict(sourceIn=True, sourceOut=2)],
                         [dict(sourceIn=0.0001, sourceOut=0.0002)]):
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                retime_cues([], segments)


class TranslationTests(unittest.TestCase):
    def test_mapping_and_unordered_list_keep_timeline_and_metadata(self):
        values = [cue(speaker="Alice", confidence=0.8, words=[]), cue("b", 1000, 2000)]
        snapshot = deepcopy(values)
        for translations in ({"a": "你好", "b": "世界"}, [{"id": "b", "text": "世界"}, {"id": "a", "text": "你好"}]):
            result = apply_translations(values, translations)
            self.assertEqual([x["text"] for x in result], ["你好", "世界"])
            self.assertEqual([{k: v for k, v in x.items() if k != "text"} for x in result], [{k: v for k, v in x.items() if k != "text"} for x in values])
        self.assertEqual(values, snapshot)

    def test_missing_extra_duplicate_and_bad_text_rejected(self):
        for translations in ({}, {"a": "ok", "b": "extra"}, {"a": None},
                             [{"id": "a", "text": "ok"}, {"id": "a", "text": "dup"}],
                             [{"id": "a"}], [{"id": "a", "text": "x", "end_ms": 2000}],
                             [{"id": "a", "text": "x", "start_ms": False}],
                             None, "translation"):
            with self.subTest(translations=translations), self.assertRaises(ValueError):
                apply_translations([cue()], translations)

    def test_empty_and_integer_ids(self):
        self.assertEqual(apply_translations([], {}), [])
        self.assertEqual(apply_translations([cue(1)], {1: "translated"}), [cue("1", text="translated")])
        self.assertEqual(apply_translations([cue()], [{"id": "a", "text": "", "start_ms": 0, "end_ms": 1000}]), [cue(text="")])


class QualityTests(unittest.TestCase):
    def test_clean_cues_have_no_issues(self):
        self.assertEqual(qc_cues([cue(), cue("b", 1000, 2000)], 2000), [])
        self.assertEqual(qc_cues([], 0), [])

    def test_structural_errors_are_reported_not_raised(self):
        values = [cue("a", 1000, 2000), cue("a", 500, 1500), {}]
        issues = qc_cues(values, 1000)
        codes = {issue["code"] for issue in issues}
        self.assertTrue({"duplicate_id", "out_of_order", "overlap", "out_of_bounds", "invalid_cue"} <= codes)
        self.assertTrue(all(issue["severity"] == "error" for issue in issues))
        self.assertEqual(issues[0]["cue_id"], "a")
        self.assertEqual(qc_cues(None)[0]["code"], "invalid_cues")
        self.assertEqual(qc_cues([], float("nan"))[0]["code"], "invalid_duration")

    def test_editorial_heuristics_and_word_confidence(self):
        values = [cue(end=100, text="x" * 43 + "\nline2\nline3", confidence=0.5),
                  cue("b", 100, 8100, " "),
                  cue("c", 8100, 9100, words=[dict(start_ms=8100, end_ms=9100, text="x", confidence=0.2)])]
        issues = qc_cues(values)
        codes = {issue["code"] for issue in issues}
        self.assertTrue({"reading_speed", "short_duration", "long_duration", "empty_text", "line_count", "line_length", "low_confidence"} <= codes)
        self.assertTrue(all(issue["severity"] == "warning" for issue in issues))
        self.assertTrue(any(issue["cue_id"] == "c" and issue["code"] == "low_confidence" for issue in issues))


if __name__ == "__main__":
    unittest.main()
