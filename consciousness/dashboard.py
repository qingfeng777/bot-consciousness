"""Loopback-only management API and bundled frontend; reads the live WAL database."""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .cloud import CloudConfig
from .journal import Journal
from .state import StateStore
from .voice import AlsaAudio, SpeechClient

MODULES = {
    'voice': '语音对话', 'knowledge': '记忆与证据', 'learning': '学习与经验',
    'motivation': '动机', 'goals': '目标', 'scene': '场景', 'self': '自身状态',
    'affect': '情绪事件', 'identity': '身份与偏好', 'skills': '技能',
    'reflexes': '反射', 'association': '联想', 'calendar': '日历',
    'perception': '知觉', 'cloud_requests': '云调用',
}


class ReadModel:
    def __init__(self, directory):
        root = Path(directory).resolve()
        if (root / 'INCOMPLETE').exists():
            raise ValueError('refusing incomplete backup')
        self.path = root / 'journal.sqlite3'
        if not self.path.exists():
            root.mkdir(parents=True, exist_ok=True)
            journal = Journal(self.path)
            try:
                StateStore(journal)
            finally:
                journal.close()

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db

    def state(self, db, module):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='module_state'").fetchone():
            return {}, 0
        row = db.execute('SELECT body,revision FROM module_state WHERE name=?', (module,)).fetchone()
        return (json.loads(row['body']), row['revision']) if row else ({}, 0)

    def summary(self):
        db = self.connect()
        try:
            db.execute('BEGIN')
            modules = []
            for key, label in MODULES.items():
                state, rev = self.state(db, key)
                collections = {k: len(v) if isinstance(v, (list, dict)) else 1 for k, v in state.items()}
                modules.append(dict(id=key, label=label, revision=rev, collections=collections))
            voice, _ = self.state(db, 'voice')
            conversations = sorted(voice.get('conversations', {}).values(), key=lambda r: r.get('created', 0), reverse=True)[:100]
            cloud, _ = self.state(db, 'cloud_requests')
            calls = cloud.get('requests', {})
            return dict(modules=modules, conversations=conversations,
                        stats={'events': db.execute('SELECT count(*) FROM events').fetchone()[0],
                               'tasks': db.execute('SELECT count(*) FROM tasks').fetchone()[0],
                               'cloud_calls': cloud.get('calls', 0),
                               'cloud_failed': sum(r.get('status') in ('failed', 'cancelled') for r in calls.values()),
                               'tts_calls': voice.get('tts_attempts', 0)}, updated=time.time())
        finally:
            db.close()

    def records(self, module, collection, page, query, record_id=""):
        if module not in MODULES:
            raise ValueError('unknown module')
        db = self.connect()
        try:
            state, revision = self.state(db, module)
        finally:
            db.close()
        value = state.get(collection, {}) if collection else state
        if isinstance(value, dict):
            rows = [dict(id=str(k), value=v) for k, v in value.items()]
        elif isinstance(value, list):
            rows = [dict(id=str(i), value=v) for i, v in enumerate(value)]
        else:
            rows = [dict(id=collection, value=value)]
        if record_id:
            rows = [r for r in rows if r["id"] == record_id]
        if query:
            rows = [r for r in rows if query.casefold() in json.dumps(r, ensure_ascii=False).casefold()]
        return dict(rows=rows[page * 30:(page + 1) * 30], total=len(rows), page=page, revision=revision)

    def events(self, before=0):
        db = self.connect()
        try:
            rows = db.execute('SELECT seq,kind,cause_id,created,payload FROM events WHERE (?=0 OR seq<?) ORDER BY seq DESC LIMIT 50',
                              (before, before)).fetchall()
            return [dict(seq=r['seq'], kind=r['kind'], cause=r['cause_id'], created=r['created'], payload=json.loads(r['payload'])) for r in rows]
        finally:
            db.close()


class VoiceController:
    def __init__(self, directory):
        self.directory = str(Path(directory).resolve())
        self.lock = threading.RLock()
        self.process = None
        self.thread = None
        self.state = 'stopped'
        self.logs = deque(maxlen=50)
        self.exit_code = None
        self.closing = False

    @staticmethod
    def readiness():
        checks = {}
        for name, check in [('audio', AlsaAudio), ('model', CloudConfig.from_env), ('speech', SpeechClient.from_env)]:
            try:
                config = check()
                checks[name] = name != 'model' or 'audio' in config.modalities
            except (ValueError, OSError, TypeError):
                checks[name] = False
            except Exception:
                checks[name] = False
        return checks

    def status(self):
        with self.lock:
            return dict(running=self.process is not None and self.process.poll() is None,
                        state=self.state, exit_code=self.exit_code, logs=list(self.logs),
                        readiness=self.readiness())

    def start(self):
        with self.lock:
            if self.closing:
                raise ValueError('管理台正在关闭')
            if self.process is not None and self.process.poll() is None:
                raise ValueError('语音会话已在运行')
            if not all(self.readiness().values()):
                raise ValueError('语音配置尚未就绪：需要音频工具、支持音频的模型和 TTS 配置')
            self.state, self.exit_code = 'starting', None
            self.process = subprocess.Popen([sys.executable, '-u', '-m', 'consciousness', 'voice',
                '--data-dir', self.directory, '--turns', '100', '--idle-timeout', '300'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            self.thread = threading.Thread(target=self._monitor, args=(self.process,), daemon=True)
            self.thread.start()

    def _monitor(self, process):
        try:
            for line in process.stdout:
                try:
                    item = json.loads(line[:16384])
                except (ValueError, TypeError):
                    continue
                if not isinstance(item, dict):
                    continue
                with self.lock:
                    self.logs.append(dict(at=time.time(), **{k: item[k] for k in ('voice', 'error', 'detail', 'note') if k in item}))
                    if self.process is process:
                        self.state = item.get('voice', 'error' if 'error' in item else self.state)
            code = process.wait()
            with self.lock:
                if self.process is process:
                    self.exit_code = code
                    self.state = 'stopped' if code in (0, 130, -signal.SIGINT) else 'error'
        finally:
            process.stdout.close()

    def stop(self):
        with self.lock:
            process, monitor = self.process, self.thread
            if process is None or process.poll() is not None:
                return
            self.state = 'stopping'
        try:
            os.killpg(process.pid, signal.SIGINT)
            process.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
        if monitor:
            monitor.join(timeout=1)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, directory, port=8765):
        self.read_model = ReadModel(directory)
        self.controller = VoiceController(directory)
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, value, content_type='application/json; charset=utf-8'):
        raw = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        # Block DNS rebinding and cross-origin reads, including browser prefetches.
        return (self.headers.get('Host') == self.server.origin.removeprefix('http://')
                and self.headers.get('Sec-Fetch-Site', 'same-origin') in ('same-origin', 'none')
                and self.headers.get('Origin', self.server.origin) == self.server.origin)

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {'error': '仅允许本地同源访问'})
        url = urlsplit(self.path)
        params = parse_qs(url.query)
        try:
            if url.path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[url.path]
                raw = files('consciousness').joinpath('web', name).read_bytes()
                if name == 'index.html':
                    raw = raw.replace(b'__CSRF__', self.server.token.encode())
                return self.send(200, raw, {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}[name])
            if url.path == '/api/overview':
                result = self.server.read_model.summary()
                result['runtime'] = self.server.controller.status()
                return self.send(200, result)
            if url.path == '/api/records':
                page = int(params.get('page', ['0'])[0])
                query = params.get('q', [''])[0]
                if not 0 <= page <= 100000 or len(query) > 200:
                    raise ValueError('invalid page or search')
                return self.send(200, self.server.read_model.records(params.get('module', [''])[0], params.get('collection', [''])[0], page, query, params.get('id', [''])[0]))
            if url.path == '/api/events':
                before = int(params.get('before', ['0'])[0])
                if before < 0:
                    raise ValueError('invalid cursor')
                return self.send(200, self.server.read_model.events(before))
            return self.send(404, {'error': '未找到资源'})
        except (ValueError, KeyError):
            self.send(400, {'error': '请求参数无效'})
        except sqlite3.Error:
            self.send(503, {'error': '数据暂时不可读，请稍后刷新'})

    def do_POST(self):
        if not self.allowed() or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.token):
            return self.send(403, {'error': '请求校验失败，请刷新页面'})
        try:
            if self.headers.get('Content-Length', '0') != '2' or self.rfile.read(2) != b'{}':
                return self.send(400, {'error': '请求正文应为 {}'})
            if self.path == '/api/voice/start':
                self.server.controller.start()
            elif self.path == '/api/voice/stop':
                self.server.controller.stop()
            else:
                return self.send(404, {'error': '未找到操作'})
            self.send(200, self.server.controller.status())
        except ValueError as exc:
            self.send(409, {'error': str(exc)})
        except OSError:
            self.send(503, {'error': '无法启动或停止语音进程'})


def serve(directory, port=8765, auto_voice=True):
    server = DashboardServer(directory, port)
    print(json.dumps({'dashboard': server.origin, 'data_dir': str(Path(directory).resolve())}), flush=True)
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        if auto_voice and all(server.controller.readiness().values()):
            server.controller.start()
        server.serve_forever(poll_interval=0.25)
    finally:
        with server.controller.lock:
            server.controller.closing = True
        server.controller.stop()
        server.server_close()
        signal.signal(signal.SIGTERM, previous)
