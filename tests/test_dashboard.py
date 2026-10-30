import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

from consciousness.cli import main
from consciousness.dashboard import DashboardServer, ReadModel, VoiceController
from consciousness.journal import Journal
from consciousness.state import StateStore
from consciousness.voice import record_turn


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = DashboardServer(self.tmp.name, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.controller.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_frontend_bundled_and_token_not_exposed_by_api(self):
        status, headers, html = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), html)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ['/app.js', '/style.css']:
            self.assertEqual(self.request(path)[0], 200)
        status, _, body = self.request('/api/overview')
        self.assertEqual(status, 200)
        self.assertNotIn(self.server.token.encode(), body)
        self.assertEqual(len(json.loads(body)['modules']), 15)
        self.assertEqual(self.request('/../../.env')[0], 404)

    def test_live_database_read_does_not_steal_writer_lock(self):
        journal = Journal(Path(self.tmp.name) / 'journal.sqlite3')
        try:
            store = StateStore(journal)
            with store.edit('knowledge', 'test') as state:
                state['claims'] = {'c1': {'value': '厨房', 'status': 'active'}}
            result = json.loads(self.request('/api/records?module=knowledge&collection=claims')[2])
            self.assertEqual(result['rows'][0]['value']['value'], '厨房')
            with store.edit('knowledge', 'changed') as state:
                state['claims']['c1']['value'] = '客厅'
            result = json.loads(self.request('/api/records?module=knowledge&collection=claims')[2])
            self.assertEqual(result['rows'][0]['value']['value'], '客厅')
        finally:
            journal.close()

    def test_pagination_search_and_event_cursor(self):
        journal = Journal(Path(self.tmp.name) / 'journal.sqlite3')
        try:
            store = StateStore(journal)
            with store.edit('learning', 'test') as state:
                state['experiences'] = {str(i): {'text': 'sample '+str(i)} for i in range(65)}
            for i in range(60):
                record_turn(store, 'turn'+str(i), heard='hello', reply='world', status='played')
        finally:
            journal.close()
        rows = json.loads(self.request('/api/records?module=learning&collection=experiences&page=2')[2])
        self.assertEqual(len(rows['rows']), 5)
        self.assertEqual(rows['total'], 65)
        exact = json.loads(self.request('/api/records?module=learning&collection=experiences&id=6')[2])
        self.assertEqual([r['id'] for r in exact['rows']], ['6'])
        self.assertEqual(json.loads(self.request('/api/records?module=learning&collection=experiences&q=sample%2064')[2])['total'], 1)
        events = json.loads(self.request('/api/events')[2])
        older = json.loads(self.request('/api/events?before='+str(events[-1]['seq']))[2])
        self.assertTrue(all(row['seq'] < events[-1]['seq'] for row in older))
        overview = json.loads(self.request('/api/overview')[2])
        self.assertEqual(len(overview['conversations']), 60)
        self.assertEqual(overview['conversations'][0]['reply'], 'world')

    def test_origin_host_and_csrf_protect_controls(self):
        for headers in [{'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'}, {'Sec-Fetch-Site': 'cross-site'}]:
            self.assertEqual(self.request('/api/overview', headers=headers)[0], 403)
        self.assertEqual(self.request('/api/voice/start', 'POST', '{}')[0], 403)
        with patch.object(self.server.controller, 'start') as start:
            self.assertEqual(self.request('/api/voice/start', 'POST', '{}', {'X-CSRF-Token': self.server.token})[0], 200)
            start.assert_called_once()
        with patch.object(self.server.controller, 'stop') as stop:
            self.assertEqual(self.request('/api/voice/stop', 'POST', '{}', {'X-CSRF-Token': self.server.token})[0], 200)
            stop.assert_called_once()

    def test_invalid_input_and_unconfigured_start(self):
        for url in ['/api/records?module=unknown', '/api/records?module=voice&page=-1', '/api/events?before=bad']:
            self.assertEqual(self.request(url)[0], 400)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.request('/api/voice/start', 'POST', '{}', {'X-CSRF-Token': self.server.token})[0], 409)
        self.assertEqual(self.request('/api/voice/start', 'POST', '{"x":1}', {'X-CSRF-Token': self.server.token})[0], 400)

    def test_voice_summary_bounded_and_failure_visible_after_restart(self):
        journal = Journal(Path(self.tmp.name) / 'journal.sqlite3')
        try:
            store = StateStore(journal)
            for i in range(205):
                record_turn(store, str(i), heard='test', status='failed', error='CLOUD_ERROR')
            self.assertEqual(len(store.read('voice')[0]['conversations']), 200)
        finally:
            journal.close()
        result = ReadModel(self.tmp.name).summary()
        self.assertEqual(len(result['conversations']), 100)
        self.assertEqual(result['conversations'][0]['status'], 'failed')


class ControllerTests(unittest.TestCase):
    def test_no_args_start_dashboard_and_no_voice_flag(self):
        with patch('consciousness.dashboard.serve') as serve:
            self.assertEqual(main([]), 0)
            serve.assert_called_once_with('local-data/voice', 8765, True)
            self.assertEqual(main(['serve', '--no-voice', '--port', '1234']), 0)
            self.assertEqual(serve.call_args.args, ('local-data/voice', 1234, False))

    def test_no_configuration_never_spawns_child(self):
        controller = VoiceController('unused')
        with patch.dict(os.environ, {}, clear=True), patch('subprocess.Popen') as process:
            with self.assertRaises(ValueError):
                controller.start()
            process.assert_not_called()

    def test_duplicate_start_and_stop_only_managed_process_group(self):
        controller = VoiceController('unused')
        process = MagicMock()
        process.pid = 4242
        process.poll.return_value = None
        controller.process = process
        with self.assertRaises(ValueError):
            controller.start()
        with patch('os.killpg') as kill:
            controller.stop()
            self.assertEqual(kill.call_args.args[0], 4242)
            process.wait.assert_called()

    def test_real_child_failure_reported_and_reaped(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            controller = VoiceController(tmp)
            with patch.object(controller, 'readiness', return_value={'audio': True, 'model': True, 'speech': True}):
                controller.start()  # Actual CLI child exits at config check, before microphone opens.
                controller.thread.join(timeout=10)
                self.assertFalse(controller.status()['running'])
                self.assertEqual(controller.status()['state'], 'error')
                self.assertEqual(controller.status()['exit_code'], 2)
                self.assertTrue(controller.status()['logs'])
            controller.stop()

    def test_real_managed_child_stops_cleanly(self):
        import subprocess
        import sys
        controller = VoiceController('unused')
        real_popen = subprocess.Popen
        script = "import signal,time,sys; signal.signal(signal.SIGINT,lambda *_: sys.exit(130)); print('{\"voice\":\"listening\"}',flush=True); time.sleep(30)"
        def fake_voice(command, **kwargs):
            return real_popen([sys.executable, '-u', '-c', script], **kwargs)
        with patch.object(controller, 'readiness', return_value={'audio': True, 'model': True, 'speech': True}), \
                patch('subprocess.Popen', side_effect=fake_voice):
            controller.start()
            try:
                deadline = time.monotonic() + 5
                while controller.status()['state'] != 'listening' and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(controller.status()['state'], 'listening')
            finally:
                controller.stop()
            self.assertFalse(controller.status()['running'])
            self.assertEqual(controller.status()['exit_code'], 130)

    def test_shutdown_disallows_new_voice_session(self):
        controller = VoiceController('unused')
        controller.closing = True
        with patch('subprocess.Popen') as process:
            with self.assertRaises(ValueError):
                controller.start()
            process.assert_not_called()

    def test_incomplete_backup_not_initialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'INCOMPLETE').touch()
            with self.assertRaises(ValueError):
                ReadModel(tmp)
