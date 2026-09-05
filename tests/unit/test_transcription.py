from array import array
from collections import deque
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from miso.audio import AudioFormat
from miso.transcription import (
    EnergySpeechDetector,
    FallbackTranscriber,
    OpenAITranscriber,
    TranscriptionError,
    TranscriptionManager,
    TranscriptionResult,
    Utterance,
    UtteranceAssembler,
    WhisperCppTranscriber,
    WhisperServerTranscriber,
    WisprFlowTranscriber,
    guess_language,
    normalized_words,
    word_error_rate,
)
import base64
import http.server
import threading
import wave


def pcm(value: int, milliseconds: int = 20) -> bytes:
    return array("h", [value] * (16 * milliseconds)).tobytes()


class UtteranceAssemblerTests(unittest.TestCase):
    def test_emits_after_vad_end_with_pre_roll(self) -> None:
        audio_format = AudioFormat(chunk_milliseconds=20)
        assembler = UtteranceAssembler(
            audio_format,
            minimum_speech_milliseconds=40,
            end_silence_milliseconds=40,
            maximum_utterance_milliseconds=1_000,
            pre_roll_milliseconds=20,
        )
        self.assertIsNone(assembler.feed(pcm(0), speech=False))
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(0), speech=False))
        utterance = assembler.feed(pcm(0), speech=False)

        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertEqual(utterance.duration_milliseconds, 100)
        self.assertFalse(utterance.truncated)
        self.assertEqual(len(utterance.pcm), audio_format.chunk_bytes * 5)

    def test_discards_short_noise_and_bounds_long_speech(self) -> None:
        assembler = UtteranceAssembler(
            AudioFormat(chunk_milliseconds=20),
            minimum_speech_milliseconds=40,
            end_silence_milliseconds=20,
            maximum_utterance_milliseconds=60,
            pre_roll_milliseconds=0,
        )
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(0), speech=False))

        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        utterance = assembler.feed(pcm(4_000), speech=True)
        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertTrue(utterance.truncated)
        self.assertEqual(utterance.duration_milliseconds, 60)

    def test_energy_detector_distinguishes_silence(self) -> None:
        detector = EnergySpeechDetector(-40)
        self.assertFalse(detector.is_speech(pcm(0)))
        self.assertTrue(detector.is_speech(pcm(4_000)))


class WhisperCppTranscriberTests(unittest.TestCase):
    def test_returns_language_timestamps_and_confidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "ggml-base-q5_1.bin"
            model.write_bytes(b"model")
            executable = root / "whisper-cli"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "prefix = pathlib.Path(sys.argv[sys.argv.index('--output-file') + 1])\n"
                "payload = {\n"
                " 'result': {'language': 'es'},\n"
                " 'transcription': [{\n"
                "  'offsets': {'from': 120, 'to': 980},\n"
                "  'text': ' apaga la luz',\n"
                "  'tokens': [\n"
                "   {'text': '<|startoftranscript|>', 'p': 0.99},\n"
                "   {'text': ' apaga', 'p': 0.8, 'offsets': {'from': 120, 'to': 500}},\n"
                "   {'text': ' la luz', 'p': 0.6, 'offsets': {'from': 500, 'to': 980}}\n"
                "  ]\n"
                " }]\n"
                "}\n"
                "prefix.with_suffix('.json').write_text(json.dumps(payload))\n"
                "print('whisper_full_with_state: auto-detected language: es (p = 0.91)', file=sys.stderr)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            transcriber = WhisperCppTranscriber(
                executable, model, threads=4, timeout_seconds=5
            )
            result = transcriber.transcribe(
                Utterance(pcm(2_000, 1_000), 16_000, 1, 1_000)
            )

        self.assertEqual(result.text, "apaga la luz")
        self.assertEqual(result.language, "es")
        self.assertEqual(result.model_language, "es")
        self.assertAlmostEqual(result.language_confidence or 0, 0.91)
        self.assertAlmostEqual(result.confidence or 0, 0.7)
        self.assertEqual(result.segments[0].start_milliseconds, 120)
        self.assertEqual(result.segments[0].tokens[1].end_milliseconds, 980)
        self.assertEqual(result.model, "ggml-base-q5_1.bin")
        self.assertGreaterEqual(result.inference_milliseconds, 0)

    def test_rejects_missing_runtime(self) -> None:
        transcriber = WhisperCppTranscriber(
            Path("/missing/whisper-cli"),
            Path("/missing/model.bin"),
            threads=1,
            timeout_seconds=1,
        )
        self.assertFalse(transcriber.available())
        with self.assertRaisesRegex(RuntimeError, "executable is unavailable"):
            transcriber.transcribe(Utterance(b"\0\0", 16_000, 1, 1))

    def test_classifies_code_switch_and_preserves_acoustic_language(self) -> None:
        transcriber = WhisperCppTranscriber(
            Path("/bin/whisper-cli"),
            Path("/models/ggml-tiny.bin"),
            threads=4,
            timeout_seconds=5,
        )
        payload = {
            "result": {"language": "es"},
            "transcription": [
                {
                    "offsets": {"from": 0, "to": 1_000},
                    "text": "set a timer for five minutes y apaga la luz",
                    "tokens": [
                        {"text": " set a timer", "p": 0.8},
                        {"text": " y apaga la luz", "p": 0.7},
                    ],
                }
            ],
        }
        result = transcriber._result(
            payload,
            "auto-detected language: es (p = 0.62)",
            Utterance(pcm(1_000, 1_000), 16_000, 1, 1_000),
            500,
        )

        self.assertEqual(result.language, "mixed")
        self.assertEqual(result.model_language, "es")
        self.assertEqual(result.language_confidence, 0.62)


class FakeAudio:
    audio_format = AudioFormat(chunk_milliseconds=20)

    def __init__(self, chunks=None, *, repeat: bool = False) -> None:
        self.pattern = tuple(
            (pcm(4_000), pcm(4_000), pcm(0), pcm(0))
            if chunks is None
            else chunks
        )
        self.chunks = deque(self.pattern)
        self.repeat = repeat

    def read_capture(self, timeout: float | None = None) -> bytes | None:
        if not self.chunks and self.repeat:
            self.chunks.extend(self.pattern)
        if self.chunks:
            return self.chunks.popleft()
        time.sleep(min(timeout or 0, 0.01))
        return None


class FakeTranscriber:
    model = Path("/models/ggml-tiny.bin")
    model_name = "ggml-tiny.bin"

    def available(self) -> bool:
        return True

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        return TranscriptionResult(
            text="enciende la luz",
            language="es",
            model_language="es",
            language_confidence=0.95,
            confidence=0.9,
            segments=(),
            audio_milliseconds=utterance.duration_milliseconds,
            inference_milliseconds=100,
            real_time_factor=1.0,
            model=self.model.name,
        )


class StubLane:
    """A transcription lane whose availability and failures are scripted."""

    model = Path("/models/stub.bin")

    def __init__(self, name: str, *, text: str = "", fails: bool = False) -> None:
        self.name = name
        self.model_name = name
        self.text = text or name
        self.fails = fails
        self.ready = True
        self.calls = 0
        self.warmed = 0

    def available(self) -> bool:
        return self.ready

    def warm_up(self) -> None:
        self.warmed += 1

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        self.calls += 1
        if self.fails:
            raise TranscriptionError(f"{self.name} is broken")
        return TranscriptionResult(
            text=self.text,
            language="en",
            model_language="en",
            language_confidence=None,
            confidence=None,
            segments=(),
            audio_milliseconds=utterance.duration_milliseconds,
            inference_milliseconds=10,
            real_time_factor=0.1,
            model=self.name,
        )


def utterance(milliseconds: int = 200) -> Utterance:
    return Utterance(
        pcm=pcm(4_000, milliseconds),
        sample_rate=16_000,
        channels=1,
        duration_milliseconds=milliseconds,
    )


class JSONStubServer:
    """A loopback HTTP server that returns one canned JSON body."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.requests: list[tuple[str, bytes]] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                stub.requests.append((self.path, self.rfile.read(length)))
                body = json.dumps(stub.payload).encode("utf-8")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                stub.requests.append((self.path, b""))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args) -> None:
                return

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "JSONStubServer":
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        return self

    def __exit__(self, *_exception) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class WisprFlowTranscriberTests(unittest.TestCase):
    def test_sends_base64_wav_and_returns_the_detected_language(self) -> None:
        payload = {
            "id": "abc",
            "text": "enciende la luz de la cocina",
            "detected_language": "es",
            "total_time": 432,
        }
        with JSONStubServer(payload) as stub:
            lane = WisprFlowTranscriber(
                "test-key", base_url=stub.url, languages=("en", "es")
            )
            self.assertTrue(lane.available())
            result = lane.transcribe(utterance())

        self.assertEqual(result.text, "enciende la luz de la cocina")
        self.assertEqual(result.language, "es")
        self.assertEqual(result.model, "wispr-flow")
        path, body = stub.requests[-1]
        self.assertEqual(path, "/api")
        sent = json.loads(body)
        self.assertEqual(sent["language"], ["en", "es"])
        audio = base64.b64decode(sent["audio"])
        self.assertEqual(audio[:4], b"RIFF")
        with wave.open(io_bytes(audio)) as opened:
            self.assertEqual(opened.getframerate(), 16_000)
            self.assertEqual(opened.getnchannels(), 1)

    def test_an_unconfigured_key_never_reaches_the_network(self) -> None:
        lane = WisprFlowTranscriber(None)
        self.assertFalse(lane.available())
        with self.assertRaises(TranscriptionError):
            lane.transcribe(utterance())

    def test_a_server_error_is_a_transcription_error(self) -> None:
        with JSONStubServer({"error": "nope"}, status=500) as stub:
            lane = WisprFlowTranscriber("test-key", base_url=stub.url)
            with self.assertRaises(TranscriptionError):
                lane.transcribe(utterance())


class OpenAITranscriberTests(unittest.TestCase):
    def test_posts_multipart_and_keeps_the_reported_language(self) -> None:
        payload = {"text": " Enciende la luz.", "language": "spanish"}
        with JSONStubServer(payload) as stub:
            lane = OpenAITranscriber("sk-test", base_url=stub.url)
            result = lane.transcribe(utterance())

        self.assertEqual(result.text, "Enciende la luz.")
        # whisper-1 names the language in English; a raw "spanish" here would
        # make the conversation answer a Spanish question in English.
        self.assertEqual(result.language, "es")
        self.assertEqual(result.model, "whisper-1")
        path, body = stub.requests[-1]
        self.assertEqual(path, "/audio/transcriptions")
        self.assertIn(b'name="model"', body)
        self.assertIn(b"whisper-1", body)
        self.assertIn(b"verbose_json", body)
        self.assertIn(b'filename="utterance.wav"', body)
        # No language is pinned: forcing one would transcribe the other into
        # nonsense in a bilingual house.
        self.assertNotIn(b'name="language"', body)

    def test_a_model_that_reports_no_language_falls_back_to_the_words(
        self,
    ) -> None:
        with JSONStubServer({"text": "pon un temporizador de cinco minutos"}) as stub:
            lane = OpenAITranscriber(
                "sk-test",
                base_url=stub.url,
                model="gpt-4o-mini-transcribe",
                response_format="json",
            )
            result = lane.transcribe(utterance())

        self.assertEqual(result.language, "es")

    def test_the_provider_is_a_base_url_not_a_code_change(self) -> None:
        with JSONStubServer({"text": "hello", "language": "english"}) as stub:
            lane = OpenAITranscriber(
                "gsk-test", base_url=stub.url, model="whisper-large-v3-turbo"
            )
            result = lane.transcribe(utterance())

        self.assertEqual(result.model, "whisper-large-v3-turbo")
        self.assertEqual(result.language, "en")

    def test_an_unconfigured_key_never_reaches_the_network(self) -> None:
        lane = OpenAITranscriber(None)
        self.assertFalse(lane.available())
        with self.assertRaises(TranscriptionError):
            lane.transcribe(utterance())

    def test_a_server_error_is_a_transcription_error(self) -> None:
        with JSONStubServer({"error": "nope"}, status=500) as stub:
            lane = OpenAITranscriber("sk-test", base_url=stub.url)
            with self.assertRaises(TranscriptionError):
                lane.transcribe(utterance())


class LanguageGuessTests(unittest.TestCase):
    def test_function_words_decide_the_language(self) -> None:
        self.assertEqual(guess_language("enciende la luz de la cocina"), "es")
        self.assertEqual(guess_language("turn on the kitchen light"), "en")
        self.assertEqual(guess_language("anade leche a la lista"), "es")

    def test_no_signal_returns_no_verdict(self) -> None:
        # Better to leave the caller's default in charge than invent one.
        self.assertEqual(guess_language("ok"), "")
        self.assertEqual(guess_language(""), "")


class WhisperServerTranscriberTests(unittest.TestCase):
    def test_posts_multipart_audio_to_the_inference_endpoint(self) -> None:
        with JSONStubServer({"text": " turn on the kitchen light"}) as stub:
            lane = WhisperServerTranscriber(
                stub.url, model=Path("/models/ggml-tiny.bin")
            )
            result = lane.transcribe(utterance())

        self.assertEqual(result.text, "turn on the kitchen light")
        self.assertEqual(result.model, "ggml-tiny.bin")
        # No language verdict is invented: the conversation drops transcripts
        # whose language is outside the household's, and a guess here would
        # throw away good audio.
        self.assertEqual(result.model_language, "")
        path, body = stub.requests[-1]
        self.assertEqual(path, "/inference")
        self.assertIn(b'name="file"; filename="utterance.wav"', body)
        self.assertIn(b"RIFF", body)


class FallbackTranscriberTests(unittest.TestCase):
    def test_the_first_ready_lane_answers(self) -> None:
        fast, slow = StubLane("wispr-flow"), StubLane("whisper-server")
        chain = FallbackTranscriber((fast, slow))

        self.assertEqual(chain.transcribe(utterance()).text, "wispr-flow")
        self.assertEqual(chain.last_lane, "wispr-flow")
        self.assertEqual(slow.calls, 0)

    def test_a_failing_lane_falls_through_and_then_sits_out(self) -> None:
        broken = StubLane("wispr-flow", fails=True)
        local = StubLane("whisper-server")
        chain = FallbackTranscriber((broken, local), cooldown_seconds=60)

        self.assertEqual(chain.transcribe(utterance()).text, "whisper-server")
        self.assertEqual(chain.transcribe(utterance()).text, "whisper-server")
        # Paying the hosted lane's timeout on every sentence once the uplink
        # is gone is slower than having no hosted lane at all.
        self.assertEqual(broken.calls, 1)
        self.assertEqual(chain.lane_counts(), {"whisper-server": 2})

    def test_every_lane_failing_raises_with_both_reasons(self) -> None:
        chain = FallbackTranscriber(
            (StubLane("wispr-flow", fails=True), StubLane("whisper-cli", fails=True))
        )
        with self.assertRaises(TranscriptionError) as caught:
            chain.transcribe(utterance())
        self.assertIn("wispr-flow", str(caught.exception))
        self.assertIn("whisper-cli", str(caught.exception))

    def test_only_the_lane_that_would_answer_is_warmed(self) -> None:
        first, second = StubLane("wispr-flow"), StubLane("whisper-server")
        chain = FallbackTranscriber((first, second))
        chain.warm_up()

        self.assertEqual((first.warmed, second.warmed), (1, 0))

    def test_an_unavailable_lane_is_skipped_without_being_called(self) -> None:
        hosted = StubLane("wispr-flow")
        hosted.ready = False
        local = StubLane("whisper-server")
        chain = FallbackTranscriber((hosted, local))

        self.assertEqual(chain.transcribe(utterance()).text, "whisper-server")
        self.assertEqual(hosted.calls, 0)
        self.assertEqual(chain.model_name, "whisper-server")


def io_bytes(data: bytes):
    import io

    return io.BytesIO(data)


class TranscriptionManagerTests(unittest.TestCase):
    def test_processes_post_vad_utterance_and_reports_bounded_status(self) -> None:
        audio = FakeAudio()
        manager = TranscriptionManager(
            enabled=True,
            audio=audio,
            transcriber=FakeTranscriber(),
            detector=EnergySpeechDetector(-40),
            assembler=UtteranceAssembler(
                audio.audio_format,
                minimum_speech_milliseconds=40,
                end_silence_milliseconds=40,
                maximum_utterance_milliseconds=1_000,
                pre_roll_milliseconds=0,
            ),
            result_capacity=2,
        )
        manager.start()
        result = manager.get_result(timeout=1)
        started = manager.get_activity(timeout=1)
        ended = manager.get_activity(timeout=1)
        manager.stop()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "enciende la luz")
        self.assertIsNotNone(started)
        self.assertIsNotNone(ended)
        assert started is not None and ended is not None
        self.assertEqual((started.kind, ended.kind), ("started", "ended"))
        status = manager.status()
        self.assertEqual(status["processed"], 1)
        self.assertNotIn("enciende", json.dumps(status))

    def test_a_gated_worker_transcribes_nothing_until_the_gate_opens(self) -> None:
        # The regression this guards: the worker transcribed every sound in
        # the house and handed the state machine whatever the room said.
        audio = FakeAudio(repeat=True)
        transcriber = FakeTranscriber()
        manager = TranscriptionManager(
            enabled=True,
            audio=audio,
            transcriber=transcriber,
            detector=EnergySpeechDetector(-40),
            assembler=UtteranceAssembler(
                audio.audio_format,
                minimum_speech_milliseconds=40,
                end_silence_milliseconds=40,
                maximum_utterance_milliseconds=1_000,
                pre_roll_milliseconds=0,
            ),
            result_capacity=2,
            gated=True,
        )
        manager.start()
        try:
            self.assertIsNone(manager.get_result(timeout=0.3))
            self.assertFalse(manager.gate_open)
            self.assertGreater(manager.status()["gated_out_chunks"], 0)

            manager.open_gate()
            result = manager.get_result(timeout=1)
        finally:
            manager.stop()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "enciende la luz")

    def test_an_ungated_worker_keeps_its_always_on_behaviour(self) -> None:
        audio = FakeAudio()
        manager = TranscriptionManager(
            enabled=True,
            audio=audio,
            transcriber=FakeTranscriber(),
            detector=EnergySpeechDetector(-40),
            assembler=UtteranceAssembler(
                audio.audio_format,
                minimum_speech_milliseconds=40,
                end_silence_milliseconds=40,
                maximum_utterance_milliseconds=1_000,
                pre_roll_milliseconds=0,
            ),
            result_capacity=2,
        )
        manager.close_gate()
        manager.start()
        try:
            self.assertTrue(manager.gate_open)
            self.assertIsNotNone(manager.get_result(timeout=1))
        finally:
            manager.stop()

    def test_reports_short_discarded_utterance_without_transcribing(self) -> None:
        audio = FakeAudio((pcm(4_000), pcm(0), pcm(0)))
        manager = TranscriptionManager(
            enabled=True,
            audio=audio,
            transcriber=FakeTranscriber(),
            detector=EnergySpeechDetector(-40),
            assembler=UtteranceAssembler(
                audio.audio_format,
                minimum_speech_milliseconds=40,
                end_silence_milliseconds=40,
                maximum_utterance_milliseconds=1_000,
                pre_roll_milliseconds=0,
            ),
            result_capacity=2,
        )
        manager.start()
        started = manager.get_activity(timeout=1)
        discarded = manager.get_activity(timeout=1)
        result = manager.get_result(timeout=0.05)
        manager.stop()

        self.assertIsNotNone(started)
        self.assertIsNotNone(discarded)
        assert started is not None and discarded is not None
        self.assertEqual((started.kind, discarded.kind), ("started", "discarded"))
        self.assertIsNone(result)
        self.assertEqual(manager.status()["processed"], 0)


class BenchmarkMetricTests(unittest.TestCase):
    def test_normalizes_accents_and_calculates_wer(self) -> None:
        self.assertEqual(
            normalized_words("¡Enciende la lámpara!"),
            ("enciende", "la", "lampara"),
        )
        self.assertEqual(word_error_rate("set a timer", "set the timer"), 1 / 3)
        self.assertEqual(
            word_error_rate("pon un temporizador", "pon un temporizador"), 0
        )

    def test_fixture_shape_is_serializable(self) -> None:
        result = {"wer": word_error_rate("hello", "hello")}
        self.assertEqual(json.loads(json.dumps(result)), {"wer": 0.0})


if __name__ == "__main__":
    unittest.main()
