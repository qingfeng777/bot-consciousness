import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from consciousness.cli import main
from consciousness.cloud import CloudConfig, CloudError
from consciousness.domain import InvalidInput
from consciousness.journal import Journal
from consciousness.state import StateStore
from consciousness.voice import (AlsaAudio, FRAME_BYTES, Segmenter, SpeechClient,
                                 reply, rms, validate_wav, voice_session, wav_bytes)
from test_cloud import Server

SILENT = b'\0' * FRAME_BYTES
LOUD = b'\xe8\x03' * (FRAME_BYTES // 2)


class SegmentationTests(unittest.TestCase):
    def test_silence_never_produces_a_request(self):
        detector = Segmenter()
        self.assertTrue(all(detector.feed(SILENT) is None for _ in range(2000)))
        self.assertEqual(rms(SILENT), 0)

    def test_phrase_includes_preroll_and_stops_at_silence(self):
        detector = Segmenter()
        for frame in [SILENT] * 12 + [LOUD] * 15 + [SILENT] * 39:
            self.assertIsNone(detector.feed(frame))
        self.assertEqual(detector.feed(SILENT), SILENT * 10 + LOUD * 15 + SILENT * 40)

    def test_short_noise_rejected_and_continuous_audio_bounded(self):
        detector = Segmenter(maximum=1)
        results = [detector.feed(frame) for frame in [LOUD] * 2 + [SILENT] * 45]
        self.assertTrue(all(x is None for x in results))
        results = [detector.feed(LOUD) for _ in range(100)]
        self.assertTrue(any(results))
        self.assertTrue(all(len(x) <= FRAME_BYTES * 50 for x in results if x))

    def test_wav_validation_rejects_invalid_and_truncated_payload(self):
        raw = wav_bytes(LOUD * 5)
        self.assertEqual(validate_wav(raw), raw)
        for bad in [b'not audio', raw[:-2], wav_bytes(b'')]:
            with self.assertRaises(CloudError):
                validate_wav(bad)

    def test_missing_config_does_not_open_microphone(self):
        with patch.dict(os.environ, {}, clear=True), patch('consciousness.voice.shutil.which', return_value='/test'), \
                patch.object(AlsaAudio, 'listen', new_callable=AsyncMock) as listen, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['voice']), 2)
            listen.assert_not_called()

    def test_tts_credentials_explicit_and_redacted(self):
        env = {'BOT_TTS_BASE_URL': 'https://example.invalid/v1', 'BOT_TTS_MODEL': 'speech',
               'BOT_TTS_VOICE': 'voice', 'BOT_TTS_API_KEY_ENV': 'EXISTING_KEY', 'EXISTING_KEY': 'private-key'}
        with patch.dict(os.environ, env, clear=True):
            self.assertNotIn('private-key', repr(SpeechClient.from_env().config))
        with patch.dict(os.environ, {'BOT_MODEL_API_KEY': 'unrelated'}, clear=True):
            with self.assertRaises(InvalidInput):
                SpeechClient.from_env()


class VoiceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_speech_http_contract_and_bad_audio(self):
        server = Server()
        try:
            server.raw = wav_bytes(LOUD)
            speech = SpeechClient(server.config(), 'test-voice')
            self.assertEqual(await speech.speak('你好'), server.raw)
            call = server.calls[0]
            self.assertEqual(call['path'], '/v1/audio/speech')
            self.assertEqual(call['body'], {'model': 'test-cloud', 'voice': 'test-voice', 'input': '你好', 'response_format': 'wav'})
            self.assertEqual(call['headers']['Authorization'], 'Bearer test-key')
            server.raw = b'bad'
            with self.assertRaises(CloudError):
                await speech.speak('hello')
            server.status = 302
            with self.assertRaises(CloudError):
                await speech.speak('hello')
            self.assertEqual(len(server.calls), 3)
        finally:
            server.close()

    async def test_speech_timeout_and_cancellation(self):
        import threading
        server = Server()
        try:
            server.release = threading.Event()
            speech = SpeechClient(server.config(timeout=0.05), 'test')
            with self.assertRaises(asyncio.TimeoutError):
                await speech.speak('hello')
            task = asyncio.create_task(speech.speak('hello'))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            server.close()

    async def test_reply_clarification_does_not_invent_answer(self):
        service = SimpleNamespace(infer=AsyncMock(return_value={'status': 'needs_clarification', 'questions': ['请再说一次。']}))
        self.assertEqual(await reply(service, Path('input.wav'), 'turn'), '请再说一次。')
        self.assertEqual(service.infer.await_count, 1)

    async def test_session_serial_order_cleanup_and_persistent_budget(self):
        events = []
        audio = SimpleNamespace()
        async def listen(*args):
            events.append('listen')
            return wav_bytes(LOUD)
        async def play(path):
            events.append('play')
            self.assertEqual(path.read_bytes(), wav_bytes(LOUD))
        audio.listen, audio.play = listen, play
        paths = []
        async def answer(service, path, turn):
            events.append('answer')
            paths.append(path)
            self.assertTrue(path.exists())
            return '你好'
        async def speak(text):
            events.append('tts')
            return wav_bytes(LOUD)
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(capture_device='default', playback_device='default', threshold=600,
                                   silence=0.8, max_utterance=15, turns=1, idle_timeout=30, check=False, data_dir=tmp)
            with patch('consciousness.voice.AlsaAudio', return_value=audio), \
                 patch('consciousness.voice.CloudConfig.from_env', return_value=CloudConfig('https://example.invalid', 'test', 'key', modalities=('text','audio'))), \
                 patch('consciousness.voice.SpeechClient.from_env', return_value=SimpleNamespace(speak=speak)), \
                 patch('consciousness.voice.reply', side_effect=answer):
                self.assertEqual(await voice_session(args, lambda _: None), 0)
            self.assertEqual(events, ['listen', 'answer', 'tts', 'play'])
            self.assertFalse(paths[0].exists())
            journal = Journal(str(Path(tmp) / 'journal.sqlite3'))
            try:
                state = StateStore(journal).read('voice')[0]
                self.assertEqual(state['tts_attempts'], 1)
                self.assertEqual(state['last_turn']['status'], 'played')
            finally:
                journal.close()

    async def test_cognition_voice_reply_validates_both_cloud_results(self):
        from consciousness.cloud_cognition import CloudCognition
        server = Server()
        journal = Journal(':memory:')
        try:
            client = __import__('consciousness.cloud', fromlist=['CloudClient']).CloudClient(
                server.config(modalities=('text', 'audio')))
            async def complete(purpose, messages):
                if purpose == 'perception':
                    data = {'observations': [{'text': '你好', 'evidence': ['voice-test'], 'uncertainty': 'observed'}], 'missing': []}
                else:
                    data = {'text': '你好，有什么可以帮你？', 'evidence': ['voice-test']}
                return {'content': json.dumps({'status': 'ok', 'data': data, 'questions': []}), 'usage': {}}
            client.complete = complete
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / 'input.wav'
                source.write_bytes(wav_bytes(LOUD))
                store = StateStore(journal)
                text = await reply(CloudCognition(store, client), source, 'test')
                self.assertEqual(text, '你好，有什么可以帮你？')
                self.assertEqual(store.read('cloud_requests')[0]['calls'], 2)
        finally:
            journal.close()
            server.close()

    async def test_uncertain_perception_only_asks_for_repetition(self):
        service = SimpleNamespace(infer=AsyncMock(return_value={
            'status': 'ok', 'data': {'observations': [{'text': '猜测', 'uncertainty': 'uncertain'}], 'missing': []}}))
        self.assertEqual(await reply(service, Path('input.wav'), 'turn'), '我没听清楚，请再说一次。')
        self.assertEqual(service.infer.await_count, 1)

    async def test_capture_cancel_terminates_child(self):
        process = SimpleNamespace(returncode=None, stdout=SimpleNamespace(readexactly=AsyncMock(side_effect=asyncio.CancelledError)),
                                  terminate=lambda: None, wait=AsyncMock(return_value=0))
        with patch('consciousness.voice.shutil.which', return_value='/test'), \
                patch('asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process):
            with self.assertRaises(asyncio.CancelledError):
                await AlsaAudio().listen(Segmenter())
            process.wait.assert_awaited()
