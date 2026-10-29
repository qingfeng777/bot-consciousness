"""Linux half-duplex voice conversation: ALSA capture, cloud reasoning/TTS, ALSA playback."""
import array
import asyncio
from collections import deque
import http.client
import io
import math
import os
from pathlib import Path
import shutil
import ssl
import sys
import tempfile
from urllib.parse import urlsplit
import uuid
import wave

from .cloud import CloudClient, CloudConfig, CloudError
from .cloud_cognition import CloudCognition
from .domain import InvalidInput, canonical
from .journal import Journal
from .state import StateStore

RATE = 16000
FRAME_BYTES = 640  # 20 ms, mono signed 16-bit PCM


def rms(frame):
    values = array.array('h', frame)
    if sys.byteorder != 'little':
        values.byteswap()
    return math.sqrt(sum(x * x for x in values) / max(1, len(values)))


class Segmenter:
    """Energy gate with pre-roll; not a learned speech/noise classifier."""
    def __init__(self, threshold=600, silence=0.8, maximum=15):
        if not 1 <= threshold <= 32767 or not 0.2 <= silence <= 3 or not 1 <= maximum <= 30:
            raise InvalidInput('invalid voice segmentation limits')
        self.threshold, self.silence, self.maximum = threshold, round(silence * 50), round(maximum * 50)
        self.pre = deque(maxlen=10)
        self.frames = []
        self.quiet = self.voiced = 0

    def feed(self, frame):
        if len(frame) != FRAME_BYTES:
            raise InvalidInput('invalid microphone frame')
        loud = rms(frame) >= self.threshold
        if not self.frames:
            if not loud:
                self.pre.append(frame)
                return None
            self.frames = list(self.pre)
            self.pre.clear()
        self.frames.append(frame)
        self.voiced += int(loud)
        self.quiet = 0 if loud else self.quiet + 1
        if self.quiet >= self.silence or len(self.frames) >= self.maximum:
            result = b''.join(self.frames) if self.voiced >= 10 else None
            self.frames, self.quiet, self.voiced = [], 0, 0
            return result
        return None


def wav_bytes(pcm):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm)
    return out.getvalue()


def validate_wav(raw):
    if len(raw) > 16 * 1024 * 1024:
        raise CloudError('speech audio exceeds 16 MiB')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as f:
            if (f.getnchannels() not in (1, 2) or f.getsampwidth() != 2
                    or not 8000 <= f.getframerate() <= 96000 or not 0 < f.getnframes() <= f.getframerate() * 120):
                raise ValueError()
            if len(f.readframes(f.getnframes())) != f.getnframes() * f.getnchannels() * 2:
                raise ValueError()
    except (wave.Error, EOFError, ValueError):
        raise CloudError('speech endpoint must return a complete PCM16 WAV of at most 120 seconds') from None
    return raw


async def stop_process(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


class AlsaAudio:
    def __init__(self, capture='default', playback='default'):
        if not shutil.which('arecord') or not shutil.which('aplay'):
            raise InvalidInput('voice requires Linux ALSA tools arecord and aplay (alsa-utils)')
        self.capture, self.playback = capture, playback

    async def listen(self, segmenter, idle=30):
        process = await asyncio.create_subprocess_exec(
            'arecord', '-q', '-D', self.capture, '-t', 'raw', '-f', 'S16_LE', '-r', str(RATE), '-c', '1',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            # Bound each listening window, including device stalls and silence.
            async def read():
                while True:
                    frame = await process.stdout.readexactly(FRAME_BYTES)
                    pcm = segmenter.feed(frame)
                    if pcm:
                        return wav_bytes(pcm)
            return await asyncio.wait_for(read(), idle)
        except asyncio.IncompleteReadError:
            raise InvalidInput('microphone capture failed; check ALSA device and permissions') from None
        except asyncio.TimeoutError:
            return None
        finally:
            await stop_process(process)

    async def play(self, path):
        process = await asyncio.create_subprocess_exec('aplay', '-q', '-D', self.playback, str(path),
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
        try:
            code = await asyncio.wait_for(process.wait(), 125)
            if code:
                raise InvalidInput('speaker playback failed; check ALSA output device')
        finally:
            await stop_process(process)


class SpeechClient(CloudClient):
    def __init__(self, config, voice):
        super().__init__(config)
        if not isinstance(voice, str) or not voice.strip() or len(voice) > 128:
            raise InvalidInput('BOT_TTS_VOICE must be configured')
        self.voice = voice

    @classmethod
    def from_env(cls):
        # Separate explicit credentials: never send the model key to a different TTS host implicitly.
        key_name = os.environ.get('BOT_TTS_API_KEY_ENV', '')
        import re
        if key_name and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', key_name):
            raise InvalidInput('BOT_TTS_API_KEY_ENV must name an environment variable')
        if key_name and os.environ.get('BOT_TTS_API_KEY'):
            raise InvalidInput('configure only one TTS key source')
        config = CloudConfig(os.environ.get('BOT_TTS_BASE_URL', ''), os.environ.get('BOT_TTS_MODEL', ''),
                             os.environ.get(key_name or 'BOT_TTS_API_KEY', ''),
                             timeout=float(os.environ.get('BOT_TTS_TIMEOUT', '30')))
        return cls(config, os.environ.get('BOT_TTS_VOICE', ''))

    async def speak(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise InvalidInput('spoken reply must contain 1..2000 characters')
        return await self._request(canonical(dict(model=self.config.model, voice=self.voice,
                                                 input=text, response_format='wav')).encode())

    def _send(self, body, control):
        url = urlsplit(self.config.base_url)
        connection = (http.client.HTTPSConnection(url.hostname, url.port, timeout=self.config.timeout,
                                                   context=ssl.create_default_context()) if url.scheme == 'https'
                      else http.client.HTTPConnection(url.hostname, url.port, timeout=self.config.timeout))
        with control.lock:
            if control.cancelled:
                raise CloudError('speech request cancelled before send')
            control.connection = connection
        try:
            connection.connect()
            with control.lock:
                control.socket = connection.sock
                if control.cancelled:
                    raise CloudError('speech request cancelled before send')
            connection.request('POST', url.path.rstrip('/') + '/audio/speech', body,
                               {'Authorization': 'Bearer ' + self.config.api_key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            if response.status != 200:
                raise CloudError('speech endpoint returned HTTP ' + str(response.status))
            return validate_wav(response.read(16 * 1024 * 1024 + 1))
        except CloudError:
            raise
        except Exception:
            raise CloudError('speech transport or response failed') from None
        finally:
            connection.close()


async def reply(service, audio, turn):
    evidence = 'voice-' + turn
    observed = await service.infer('perception', turn + '-listen',
        {'task': '逐字理解本段用户语音，保留问题、否定和语气；听不清时请求澄清。'},
        scope='voice', evidence_ids=[evidence], attachments=[{'id': evidence, 'type': 'audio', 'path': str(audio)}])
    if observed['status'] == 'needs_clarification':
        return ' '.join(observed['questions'])[:2000]
    if (not observed['data']['observations'] or observed['data']['missing']
            or any(item['uncertainty'] != 'observed' for item in observed['data']['observations'])):
        return '我没听清楚，请再说一次。'
    result = await service.infer('expression', turn + '-reply',
        {'task': '用简短自然的中文回答这段用户语音；最多300字。只做对话，不执行设备操作。不确定时说明。',
         'speech_observations': observed['data'], 'capabilities': ['spoken_conversation']},
        scope='voice', evidence_ids=[evidence])
    return (result['data']['text'] if result['status'] == 'ok' else ' '.join(result['questions']))[:2000]


async def voice_session(args, emit):
    audio = AlsaAudio(args.capture_device, args.playback_device)
    Segmenter(args.threshold, args.silence, args.max_utterance)
    if not 1 <= args.turns <= 100 or not 1 <= args.idle_timeout <= 300:
        raise InvalidInput('voice turns must be 1..100 and idle timeout 1..300 seconds')
    if args.check:
        emit({'audio_tools_available': True, 'device_open_tested': False, 'cloud_tested': False})
        return 0
    config = CloudConfig.from_env()
    if 'audio' not in config.modalities:
        raise InvalidInput('voice requires BOT_MODEL_MODALITIES=text,audio and an audio-capable perception model')
    tts = SpeechClient.from_env()  # Validate everything before opening microphone.
    root = Path(args.data_dir)
    if (root / 'INCOMPLETE').exists():
        raise InvalidInput('refusing incomplete backup')
    root.mkdir(parents=True, exist_ok=True)
    journal = Journal(str(root / 'journal.sqlite3'))
    try:
        store = StateStore(journal)
        service = CloudCognition(store, CloudClient(config))
        for _ in range(args.turns):
            attempts = store.read('cloud_requests')[0].get('calls', 0)
            if attempts + 2 > config.max_calls:
                raise InvalidInput('voice requires budget for two more cloud model calls')
            # Separate persistent TTS attempt budget, including failures and cancellations.
            if store.read('voice')[0].get('tts_attempts', 0) >= config.max_calls:
                raise InvalidInput('voice speech request budget exhausted')
            emit({'voice': 'listening', 'note': '录音将发送到配置的云模型；Ctrl+C 停止。播放为 AI 合成语音。'})
            raw = await audio.listen(Segmenter(args.threshold, args.silence, args.max_utterance), args.idle_timeout)
            if raw is None:
                emit({'voice': 'idle_timeout'})
                return 0
            turn = uuid.uuid4().hex
            # Capture has stopped before inference/playback: no self-listening loop.
            with tempfile.TemporaryDirectory(prefix='bot-voice-') as tmp:
                source, target = Path(tmp) / 'input.wav', Path(tmp) / 'reply.wav'
                source.write_bytes(raw)
                emit({'voice': 'thinking'})
                text = await reply(service, source, turn)
                with store.edit('voice', turn) as state:
                    state['tts_attempts'] = state.get('tts_attempts', 0) + 1
                    state['last_turn'] = {'id': turn, 'status': 'synthesis_pending'}
                target.write_bytes(await tts.speak(text))
                with store.edit('voice', turn + '-play') as state:
                    state['last_turn']['status'] = 'playback_pending'
                emit({'voice': 'speaking', 'text': text})
                await audio.play(target)
                with store.edit('voice', turn + '-done') as state:
                    state['last_turn']['status'] = 'played'
                emit({'voice': 'played'})
                await asyncio.sleep(0.25)
        return 0
    finally:
        journal.close()
