"""Offline unit tests: no model downloads, executables, or real HTTP calls."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import wave

from video_agent import providers as p


CUES = [
    {"id": "first", "start_ms": 10, "end_ms": 900, "text": "Hello", "speaker": "A"},
    {"id": 2, "start_ms": 1200, "end_ms": 2200, "text": "World"},
]
ENV = {"VIDEO_AGENT_API_BASE": "https://example.invalid/v1", "VIDEO_AGENT_API_KEY": "secret-test-token", "VIDEO_AGENT_MODEL": "test-model"}


def response(items):
    body = {"choices": [{"message": {"content": json.dumps({"cues": items})}}]}
    return io.BytesIO(json.dumps(body).encode())


class ValidationTests(unittest.TestCase):
    def test_bad_cues(self):
        bad = [None, [{}], [dict(CUES[0], id=True)], [CUES[0], CUES[0]],
               [dict(CUES[0], start_ms=-1)], [dict(CUES[0], end_ms=10)],
               [dict(CUES[0], start_ms=1.5)], [dict(CUES[0], text=" ")]]
        for cues in bad:
            with self.subTest(cues=cues), self.assertRaises(ValueError):
                p.translate(cues, "es")

    def test_unknown_providers(self):
        with self.assertRaisesRegex(p.ProviderError, "Unsupported translation"):
            p.translate(CUES, "es", provider="fake")
        with self.assertRaisesRegex(p.ProviderError, "Unsupported synthesis"):
            p.synthesize(CUES, Path("unused"), provider="fake")

    def test_timeout_and_batch_limits(self):
        for timeout in (0, -1, float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                p.translate(CUES, "es", timeout=timeout)
        for size in (0, 51, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                p.translate(CUES, "es", batch_size=size)


class ASRTests(unittest.TestCase):
    def test_lazy_dependency_failure(self):
        with patch.object(p.importlib, "import_module", side_effect=ImportError), self.assertRaisesRegex(p.ProviderError, "not installed"):
            p._transcribe_local("input.wav", "small", None, False)

    def test_local_files_only_and_ms_output(self):
        module = Mock()
        module.WhisperModel.return_value.transcribe.return_value = (
            iter([SimpleNamespace(start=0.25, end=1.75, text=" Hello ")]), SimpleNamespace(language="en"))
        with patch.object(p.importlib, "import_module", return_value=module):
            result = p._transcribe_local("input.wav", "small", None, False)
        module.WhisperModel.assert_called_once_with("small", device="cpu", compute_type="int8", local_files_only=True)
        self.assertEqual(result, {"cues": [{"id": 1, "start_ms": 250, "end_ms": 1750, "text": "Hello"}], "language": "en", "provider": "faster-whisper", "model": "small"})
        module.WhisperModel.return_value.transcribe.assert_called_once_with("input.wav", language=None, word_timestamps=True)


    def test_invalid_word_alignment_keeps_cue_and_flags_drop(self):
        words = [SimpleNamespace(start=0.1, end=3.0, word="bad", probability=0.9)]
        segment = SimpleNamespace(start=0.25, end=1.75, text="Hello", avg_logprob=-0.5, words=words)
        module = Mock()
        module.WhisperModel.return_value.transcribe.return_value = ([segment], SimpleNamespace(language="en"))
        with patch.object(p.importlib, "import_module", return_value=module):
            result = p._transcribe_local("input.wav", "small", None, False)
        cue = result["cues"][0]
        self.assertEqual((cue["start_ms"], cue["end_ms"], cue["text"]), (250, 1750, "Hello"))
        self.assertNotIn("words", cue)
        self.assertTrue(result["word_timestamps_dropped"])

    def test_word_timestamps_and_confidence(self):
        from video_agent.subtitles import validate_cues
        words = [SimpleNamespace(start=0.249, end=0.8, word=" Hello", probability=0.9),
                 SimpleNamespace(start=0.8, end=1.751, word=" world", probability=0.8)]
        segment = SimpleNamespace(start=0.25, end=1.75, text="Hello world", avg_logprob=-0.5, words=words)
        module = Mock()
        module.WhisperModel.return_value.transcribe.return_value = ([segment], SimpleNamespace(language="en"))
        with patch.object(p.importlib, "import_module", return_value=module):
            cue = p._transcribe_local("input.wav", "small", None, False)["cues"][0]
        self.assertAlmostEqual(cue["confidence"], p.math.exp(-0.5))
        self.assertEqual(cue["words"], [
            {"start_ms": 250, "end_ms": 800, "text": "Hello", "confidence": 0.9},
            {"start_ms": 800, "end_ms": 1750, "text": "world", "confidence": 0.8}])
        self.assertEqual(len(validate_cues([cue])), 1)

    def test_word_zero_length_and_rounding_are_not_fabricated(self):
        cue = {"id": 1, "start_ms": 0, "end_ms": 1000, "text": "test"}
        raw = [SimpleNamespace(start=0, end=0, word="zero", probability=1),
               SimpleNamespace(start=0.1, end=0.1001, word="tiny", probability=1),
               SimpleNamespace(start=0.2, end=0.5, word="real", probability=None),
               SimpleNamespace(start=0.499, end=0.8, word="next", probability=0.7)]
        words = p._asr_words(raw, cue)
        self.assertEqual(words, [{"start_ms": 200, "end_ms": 500, "text": "real"},
                                 {"start_ms": 500, "end_ms": 800, "text": "next", "confidence": 0.7}])

    def test_invalid_word_alignment_is_not_replaced(self):
        cue = {"id": 1, "start_ms": 0, "end_ms": 1000, "text": "test"}
        for start, end in ((0, 2), (-1, 0.5), (0.8, 0.2), (float("nan"), 0.5)):
            raw = [SimpleNamespace(start=start, end=end, word="bad", probability=0.9)]
            with self.subTest(start=start, end=end), self.assertRaises(p.ProviderError):
                p._asr_words(raw, cue)

    def test_confidence_clip_and_missing_values(self):
        self.assertEqual(p._asr_confidence(1000, log_probability=True), 1)
        self.assertEqual(p._asr_confidence(-1000, log_probability=True), 0)
        self.assertEqual(p._asr_confidence(1.1), 1)
        self.assertEqual(p._asr_confidence(-0.1), 0)
        for missing in (None, float("nan"), float("inf"), "unknown", True):
            self.assertIsNone(p._asr_confidence(missing))
            self.assertIsNone(p._asr_confidence(missing, log_probability=True))

    def test_explicit_download_flag(self):
        module = Mock()
        module.WhisperModel.return_value.transcribe.return_value = ([], SimpleNamespace(language="en"))
        with patch.object(p.importlib, "import_module", return_value=module):
            p._transcribe_local("input.wav", "small", "en", True)
        self.assertFalse(module.WhisperModel.call_args.kwargs["local_files_only"])

    def test_invalid_asr_timestamps(self):
        module = Mock()
        module.WhisperModel.return_value.transcribe.return_value = ([SimpleNamespace(start=float("nan"), end=1, text="x")], SimpleNamespace(language="en"))
        with patch.object(p.importlib, "import_module", return_value=module), self.assertRaisesRegex(p.ProviderError, "timestamps"):
            p._transcribe_local("x", "small", None, False)

    def test_transcribe_writes_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.wav"
            source.touch()
            expected = {"cues": CUES, "language": "en", "provider": "faster-whisper", "model": "small"}
            with patch.object(p, "_run_local", return_value=expected) as run:
                result = p.transcribe(source, root / "out")
            self.assertEqual(json.loads((root / "out" / "transcript.json").read_text()), result)
            self.assertFalse(run.call_args.args[1][-1])

    def test_missing_input(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(ValueError, "existing file"):
            p.transcribe(Path(temp) / "absent", Path(temp))

    def test_local_worker_timeout_terminates_process(self):
        context, receive, send, process = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = receive, send
        context.Process.return_value = process
        receive.poll.return_value = False
        process.is_alive.side_effect = [True, False]
        with patch.object(p.multiprocessing, "get_context", return_value=context), self.assertRaisesRegex(p.ProviderError, "timed out"):
            p._run_local("asr", (), 0.1)
        process.terminate.assert_called_once()
        receive.close.assert_called_once()
        process.close.assert_called_once()


class ArgosTests(unittest.TestCase):
    def test_preserves_ids_timing_extra_fields(self):
        translation = Mock()
        translation.translate.side_effect = ["Hola", "Mundo"]
        source = SimpleNamespace(code="en", get_translation=Mock(return_value=translation))
        target = SimpleNamespace(code="es")
        module = SimpleNamespace(get_installed_languages=lambda: [source, target])
        with patch.object(p.importlib, "import_module", return_value=module):
            result = p._translate_argos_local(CUES, "en", "es")
        self.assertEqual(result, [dict(CUES[0], text="Hola"), dict(CUES[1], text="Mundo")])
        self.assertEqual(CUES[0]["text"], "Hello")

    def test_missing_installed_model(self):
        module = SimpleNamespace(get_installed_languages=lambda: [])
        with patch.object(p.importlib, "import_module", return_value=module), self.assertRaisesRegex(p.ProviderError, "No installed Argos model"):
            p._translate_argos_local(CUES, "en", "es")

    def test_missing_dependency(self):
        with patch.object(p.importlib, "import_module", side_effect=ImportError), self.assertRaisesRegex(p.ProviderError, "not installed"):
            p._translate_argos_local(CUES, "en", "es")

    def test_auto_is_not_fake_detection(self):
        with self.assertRaisesRegex(p.ProviderError, "explicit source_language"):
            p._translate_argos_local(CUES, "auto", "es")

    def test_public_dispatch(self):
        expected = [dict(CUES[0], text="Hola"), dict(CUES[1], text="Mundo")]
        with patch.object(p, "_run_local", return_value=expected) as run:
            self.assertEqual(p.translate(CUES, "es", "en"), expected)
        self.assertEqual(run.call_args.args[0], "argos")


class OpenAITests(unittest.TestCase):
    def call(self, opener, **kwargs):
        with patch.dict(p.os.environ, ENV, clear=True), patch.object(p.urllib.request, "build_opener", return_value=opener):
            return p.translate(CUES, "es", provider="openai", allow_network=True, **kwargs)

    def test_network_requires_explicit_opt_in(self):
        with patch.object(p.urllib.request, "build_opener") as network, self.assertRaisesRegex(p.ProviderError, "allow_network"):
            p.translate(CUES, "es", provider="openai")
        network.assert_not_called()

    def test_parse_reorder_preserve_and_timeout(self):
        opener = Mock()
        opener.open.return_value = response([{"id": 2, "text": "Mundo"}, {"id": "first", "text": "Hola"}])
        result = self.call(opener, timeout=7)
        self.assertEqual(result, [dict(CUES[0], text="Hola"), dict(CUES[1], text="Mundo")])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-token")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        self.assertEqual(json.loads(request.data)["model"], "test-model")

    def test_batch_limit(self):
        opener = Mock()
        opener.open.side_effect = [response([{"id": "first", "text": "Hola"}]), response([{"id": 2, "text": "Mundo"}])]
        self.call(opener, batch_size=1)
        self.assertEqual(opener.open.call_count, 2)
        for call in opener.open.call_args_list:
            payload = json.loads(call.args[0].data)
            self.assertEqual(len(json.loads(payload["messages"][1]["content"])["cues"]), 1)

    def test_strict_ids_and_schema(self):
        invalid = [[], [{"id": "first", "text": "x"}],
                   [{"id": "first", "text": "x"}, {"id": "first", "text": "y"}],
                   [{"id": "first", "text": "x"}, {"id": "2", "text": "y"}],
                   [{"id": "first", "text": "x"}, {"id": True, "text": "y"}],
                   [{"id": "first", "text": "x"}, {"id": 2, "text": ""}],
                   [{"id": "first", "text": "x", "start_ms": 99}, {"id": 2, "text": "y"}]]
        for items in invalid:
            opener = Mock()
            opener.open.return_value = response(items)
            with self.subTest(items=items), self.assertRaisesRegex(p.ProviderError, "invalid/missing/duplicate"):
                self.call(opener)

    def test_incomplete_http_response(self):
        opener = Mock()
        opener.open.side_effect = p.http.client.IncompleteRead(b"partial")
        with self.assertRaisesRegex(p.ProviderError, "request failed"):
            self.call(opener)

    def test_bad_json_and_envelopes(self):
        for body in (b"not json", b"{}", b'{"choices":[]}', b'{"choices":[{"message":{"content":"not json"}}]}'):
            opener = Mock()
            opener.open.return_value = io.BytesIO(body)
            with self.subTest(body=body), self.assertRaises(p.ProviderError):
                self.call(opener)

    def test_error_does_not_expose_key(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("secret-test-token")
        with self.assertRaises(p.ProviderError) as caught:
            self.call(opener)
        self.assertNotIn("secret-test-token", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_missing_config(self):
        with patch.dict(p.os.environ, {}, clear=True), self.assertRaisesRegex(p.ProviderError, "requires VIDEO_AGENT"):
            p._api_config()

    def test_reject_unsafe_urls_and_redirects(self):
        for base in ("file:///tmp/x", "https://user:pass@example.invalid", "https://example.invalid/?token=x", "https://example.invalid/#x", "https://[broken", "https://example.invalid:bad"):
            with patch.dict(p.os.environ, dict(ENV, VIDEO_AGENT_API_BASE=base), clear=True), self.assertRaises(p.ProviderError):
                p._api_config()
        self.assertIsNone(p._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.invalid"))

    def test_response_size_limit(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"x" * (p.MAX_RESPONSE_BYTES + 1))
        with self.assertRaisesRegex(p.ProviderError, "size limit"):
            self.call(opener)


class SynthesisTests(unittest.TestCase):
    @staticmethod
    def fake_run(command, **kwargs):
        option = "--output_file" if "--output_file" in command else "-o"
        path = command[command.index(option) + 1]
        with wave.open(path, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 800)
        return subprocess.CompletedProcess(command, 0)

    def test_piper_local_model_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / "voice model.onnx"
            model.touch()
            with patch.object(p.shutil, "which", return_value="/mock/piper"), patch.object(p.subprocess, "run", side_effect=self.fake_run) as run:
                result = p.synthesize(CUES, root / "out", voice=model, timeout=4)
            self.assertEqual(json.loads(Path(result["manifest"]).read_text()), result)
            self.assertEqual(len(result["cues"]), 2)
            for original, cue in zip(CUES, result["cues"]):
                for key in original:
                    self.assertEqual(cue[key], original[key])
                self.assertEqual(cue["duration_ms"], 100)
                self.assertTrue(Path(cue["wav_path"]).is_file())
            for call in run.call_args_list:
                self.assertIsInstance(call.args[0], list)
                self.assertIn(str(model.resolve()), call.args[0])
                self.assertEqual(call.kwargs["timeout"], 4)
                self.assertNotIn("shell", call.kwargs)

    def test_reject_model_names_and_missing_binary(self):
        with self.assertRaisesRegex(p.ProviderError, "pathlib.Path"):
            p.synthesize(CUES, Path("unused"), voice="en_voice")
        with tempfile.TemporaryDirectory() as temp:
            model = Path(temp) / "voice.onnx"
            model.touch()
            with patch.object(p.shutil, "which", return_value=None), self.assertRaisesRegex(p.ProviderError, "not installed"):
                p.synthesize(CUES, Path(temp), voice=model)

    def test_synthesis_timeout_and_redacted_failure(self):
        for error in (subprocess.TimeoutExpired(["piper"], 1), subprocess.CalledProcessError(1, ["piper"], stderr="secret-test-token")):
            with patch.object(p.subprocess, "run", side_effect=error), self.assertRaises(p.ProviderError) as caught:
                p._run_command(["piper"], "text", 1)
            self.assertNotIn("secret-test-token", str(caught.exception))

    def test_invalid_wav_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model = root / "model.onnx"
            model.touch()
            with patch.object(p.shutil, "which", return_value="/mock/piper"), patch.object(p.subprocess, "run"), self.assertRaisesRegex(p.ProviderError, "valid nonempty PCM WAV"):
                p.synthesize(CUES, root, voice=model)

    def test_say_macos_and_stdin(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(p.sys, "platform", "darwin"), patch.object(p.shutil, "which", return_value="/mock/say"), patch.object(p.subprocess, "run", side_effect=self.fake_run) as run:
            result = p.synthesize(CUES[:1], Path(temp), provider="say", voice="Samantha")
        self.assertEqual(result["voice"], "Samantha")
        self.assertEqual(run.call_args.args[0][-2:], ["-f", "-"])
        self.assertEqual(run.call_args.kwargs["input"], "Hello\n")

    def test_say_platform_guard(self):
        with patch.object(p.sys, "platform", "linux"), self.assertRaisesRegex(p.ProviderError, "macOS"):
            p.synthesize(CUES, Path("unused"), provider="say")


if __name__ == "__main__":
    unittest.main()
