"""Real loopback HTTP contract tests; no paid cloud requests in the test suite."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from consciousness.agency import Goals
from consciousness.cli import main
from consciousness.cloud import CloudClient, CloudConfig, CloudError
from consciousness.cloud_cognition import CloudCognition, media_parts
from consciousness.domain import DuplicateConflict, InvalidInput, canonical
from consciousness.journal import Journal
from consciousness.knowledge import Knowledge
from consciousness.learning import Learning
from consciousness.planning import ModelGateway
from consciousness.prompting import SCHEMAS, prompt, validate_output
from consciousness.simulation import CognitiveSystem
from consciousness.state import StateStore
from consciousness.driver import SimulatedDriver
from consciousness.runtime import Runtime


def plan():
    return dict(goal='g', evidence=['e'], steps=[dict(id='s1', capability='say', arguments={'text': 'Please confirm.'}, after=[])])


def output(data=None):
    return dict(status='ok', data=plan() if data is None else data, questions=[])


def completion(value=None, finish='stop'):
    return dict(choices=[dict(finish_reason=finish, message={'content': canonical(output() if value is None else value)})],
                usage={'prompt_tokens': 20, 'completion_tokens': 30, 'total_tokens': 50})


class Server:
    def __init__(self):
        owner = self
        self.calls = []
        self.status, self.response, self.raw = 200, completion(), None
        self.received = threading.Event()
        self.release = None
        self.body_release = None
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.calls.append(dict(path=self.path, headers=dict(self.headers), body=json.loads(body)))
                owner.received.set()
                if owner.release:
                    owner.release.wait(2)
                payload = owner.raw if owner.raw is not None else canonical(owner.response).encode()
                try:
                    self.send_response(owner.status)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    if owner.body_release:
                        owner.body_release.wait(2)
                    self.wfile.write(payload)
                except OSError:
                    pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def config(self, **kwargs):
        return CloudConfig('http://127.0.0.1:' + str(self.http.server_port) + '/v1', 'test-cloud', 'test-key',
                           _allow_http=True, **kwargs)

    def close(self):
        if self.release:
            self.release.set()
        if self.body_release:
            self.body_release.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


class ConfigPromptTests(unittest.TestCase):
    def test_key_is_not_represented_and_https_is_required(self):
        config = CloudConfig('https://example.invalid/v1', 'm', 'never-show-this')
        self.assertNotIn('never-show-this', repr(config))
        for url in ['http://example.com', 'https://user:secret@example.com', 'https://example.com?key=secret', 'file:///tmp/x']:
            with self.assertRaises(InvalidInput):
                CloudConfig(url, 'm', 'key')
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(InvalidInput):
                CloudConfig.from_env()
        with self.assertRaises(InvalidInput):
            CloudConfig('https://example.invalid', 'm', 'key', extra_body={'messages': []})

    def test_explicit_provider_key_reference_and_status_redaction(self):
        env = {'BOT_MODEL_BASE_URL': 'https://example.invalid/v1',
               'BOT_MODEL_NAME': 'model', 'BOT_MODEL_API_KEY_ENV': 'EXISTING_PROVIDER_KEY',
               'EXISTING_PROVIDER_KEY': 'private-test-value'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(CloudConfig.from_env().api_key, 'private-test-value')
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(main(['cloud-status']), 0)
            self.assertEqual(json.loads(stream.getvalue()),
                             {'configuration_valid': True, 'connectivity_tested': False})
            self.assertNotIn('private-test-value', stream.getvalue())

    def test_key_reference_never_guesses_or_falls_back(self):
        env = {'BOT_MODEL_BASE_URL': 'https://example.invalid/v1',
               'BOT_MODEL_NAME': 'model', 'OPENAI_API_KEY': 'unrelated-secret'}
        for extra in [{}, {'BOT_MODEL_API_KEY_ENV': 'MISSING_KEY'},
                      {'BOT_MODEL_API_KEY_ENV': 'bad\nname'},
                      {'BOT_MODEL_API_KEY_ENV': 'OPENAI_API_KEY', 'BOT_MODEL_API_KEY': 'other-secret'}]:
            with self.subTest(extra=extra), patch.dict(os.environ, dict(env, **extra), clear=True):
                stream = io.StringIO()
                with contextlib.redirect_stderr(stream):
                    self.assertEqual(main(['cloud-status']), 2)
                self.assertNotIn('unrelated-secret', stream.getvalue())
                self.assertNotIn('other-secret', stream.getvalue())

    def test_all_prompts_have_contract_and_distinct_fingerprints(self):
        hashes = set()
        for purpose in SCHEMAS:
            text, digest = prompt(purpose)
            self.assertIn('JSON Schema', text)
            self.assertIn('needs_clarification', text)
            self.assertIn('待分析数据', text)
            hashes.add(digest)
        self.assertEqual(len(hashes), 13)

    def test_prompt_preview_requires_no_credentials(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(stream):
            self.assertEqual(main(['prompts', 'plan']), 0)
        self.assertEqual(json.loads(stream.getvalue())['purpose'], 'plan')

    def test_output_contract_rejects_unknown_fields_and_forged_evidence(self):
        for value in [dict(output(), extra=True), output(dict(plan(), evidence=['fake'])),
                      {'status': 'needs_clarification', 'data': None, 'questions': []},
                      output(dict(plan(), steps=[{'id': 's', 'capability': 'shell', 'arguments': {'text': 'x'}, 'after': []}]))]:
            with self.assertRaises(InvalidInput):
                validate_output('plan', value, ['e'], goal='g')

    def test_media_requires_enabled_modality_and_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'frame.png'
            path.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            item = dict(id='frame1', path=str(path), type='image', at=1)
            with self.assertRaises(InvalidInput):
                media_parts([item], CloudConfig('https://example.invalid', 'm', 'key'))
            parts, manifests = media_parts([item], CloudConfig('https://example.invalid', 'm', 'key', modalities=('text','image')))
            self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/png;base64,'))
            self.assertEqual(manifests[0]['at'], 1)
            self.assertNotIn(str(path), canonical(manifests))
            with self.assertRaises(InvalidInput):
                media_parts([item] * 9, CloudConfig('https://example.invalid', 'm', 'key'))

    def test_cli_cloud_performs_real_http_request_and_exports_candidate(self):
        server = Server()
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            source.write_text(canonical(dict(input={'goal':'g'}, goal={'id':'g','predicate':{'entity':'robot','attribute':'done','value':True}},
                                            evidence=[{'id':'e','source':'user','text':'Please confirm.'}])))
            stream = io.StringIO()
            with patch('consciousness.cloud.CloudConfig.from_env', return_value=server.config()), contextlib.redirect_stdout(stream):
                code = main(['cloud','--purpose','plan','--request-id','r','--input',str(source),'--data-dir',str(Path(directory)/'data')])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stream.getvalue())['data']['goal'], 'g')
            self.assertEqual(len(server.calls), 1)


class CloudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = Server()
        self.journal = Journal()
        self.store = StateStore(self.journal)
        self.knowledge = Knowledge(self.store)
        self.knowledge.evidence('e', 'user', 'Please confirm the task.')
        self.goals = Goals(self.store, self.knowledge)
        self.goals.add('g', dict(entity='robot', attribute='done', value=True))
        self.client = CloudClient(self.server.config())
        self.service = CloudCognition(self.store, self.client)

    async def asyncTearDown(self):
        self.journal.close()
        await asyncio.to_thread(self.server.close)

    async def infer(self, rid='r', **kwargs):
        return await self.service.infer('plan', rid, {'goal':'g'}, **kwargs)

    async def test_http_payload_prompt_usage_and_durable_cache(self):
        first = await self.infer()
        self.assertEqual(await self.infer(), first)
        self.assertEqual(len(self.server.calls), 1)
        call = self.server.calls[0]
        self.assertEqual(call['path'], '/v1/chat/completions')
        self.assertEqual(call['headers']['Authorization'], 'Bearer test-key')
        self.assertEqual(call['body']['response_format'], {'type':'json_object'})
        self.assertEqual(call['body']['messages'][0]['role'], 'system')
        ledger = self.store.read('cloud_requests')[0]
        self.assertEqual(ledger['calls'], 1)
        self.assertEqual(ledger['requests']['r']['usage']['total_tokens'], 50)
        self.assertNotIn('test-key', canonical(ledger))
        self.assertEqual(self.store.read('knowledge')[0].get('claims', {}), {})
        self.assertEqual(self.journal.tasks(), [])

    async def test_default_model_gateway_uses_cloud(self):
        with patch('consciousness.cloud.CloudConfig.from_env', return_value=self.server.config()):
            gateway = ModelGateway(self.store)
            result = await gateway.generate('g', {'evidence_ids':['e']}, 0)
        self.assertEqual(result, plan())
        self.assertEqual(len(self.server.calls), 1)

    async def test_http_error_does_not_leak_body_or_retry(self):
        self.server.status = 429
        self.server.raw = b'private upstream error test-key'
        with self.assertRaises(CloudError) as caught:
            await self.infer()
        self.assertNotIn('test-key', str(caught.exception))
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.store.read('cloud_requests')[0]['requests']['r']['status'], 'failed')
        with self.assertRaises(InvalidInput):
            await self.infer()
        self.assertEqual(len(self.server.calls), 1)

    async def test_no_redirect_following(self):
        self.server.status = 302
        with self.assertRaises(CloudError):
            await self.infer()
        self.assertEqual(len(self.server.calls), 1)

    async def test_truncated_refused_malformed_and_duplicate_json_rejected(self):
        for index, response in enumerate([completion(finish='length'), {'choices':[]},
                completion({'status':'ok','data':dict(plan(),evidence=['fabricated']),'questions':[]})]):
            self.server.response = response
            with self.assertRaises((CloudError, InvalidInput)):
                await self.infer('r'+str(index))
        self.server.raw = b'{"choices":[],"choices":[]}'
        with self.assertRaises(CloudError):
            await self.infer('duplicate')

    async def test_budget_is_persistent_and_failed_call_consumes_it(self):
        self.service = CloudCognition(self.store, CloudClient(self.server.config(max_calls=1)))
        self.server.status = 503
        with self.assertRaises(CloudError):
            await self.infer('one')
        restarted = CloudCognition(StateStore(self.journal), CloudClient(self.server.config(max_calls=1)))
        with self.assertRaises(InvalidInput):
            await restarted.infer('plan','two',{'goal':'g'})
        self.assertEqual(len(self.server.calls), 1)

    async def test_changed_input_and_revoked_cache_rejected(self):
        await self.infer()
        with self.assertRaises(DuplicateConflict):
            await self.service.infer('plan','r',{'goal':'g','instruction':'changed'})
        self.knowledge.withdraw('e')
        with self.assertRaises(InvalidInput):
            await self.infer(evidence_ids=['e'])
        self.assertEqual(len(self.server.calls), 1)

    async def test_inflight_context_change_rejects_cloud_return(self):
        self.server.release = threading.Event()
        task = asyncio.create_task(self.infer())
        await asyncio.to_thread(self.server.received.wait, 2)
        self.knowledge.withdraw('e')
        self.server.release.set()
        with self.assertRaises(InvalidInput):
            await task
        self.assertEqual(self.store.read('cloud_requests')[0]['requests']['r']['status'], 'failed')

    async def test_cancellation_persists_and_does_not_publish(self):
        self.server.release = threading.Event()
        task = asyncio.create_task(self.infer())
        await asyncio.to_thread(self.server.received.wait, 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.server.release.set()
        self.assertEqual(self.store.read('cloud_requests')[0]['requests']['r']['status'], 'cancelled')
        self.assertEqual(self.journal.tasks(), [])

    async def test_timeout_does_not_fallback_to_fixture(self):
        self.service = CloudCognition(self.store, CloudClient(self.server.config(timeout=0.05)))
        self.server.release = threading.Event()
        with self.assertRaises((asyncio.TimeoutError, CloudError)):
            await self.infer()
        self.server.release.set()
        self.assertEqual(self.store.read('cloud_requests')[0]['requests']['r']['status'], 'failed')
        self.assertEqual(len(self.server.calls), 1)

    async def test_timeout_releases_slot_when_http10_body_is_stalled(self):
        client = CloudClient(self.server.config(timeout=0.05,max_concurrency=1))
        self.service = CloudCognition(self.store, client)
        self.server.body_release = threading.Event()
        with self.assertRaises((asyncio.TimeoutError, CloudError)):
            await self.infer()
        for _ in range(30):
            if client._slots._value == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(client._slots._value, 1)
        self.server.body_release.set()

    async def test_waiting_for_transport_slot_has_a_deadline(self):
        client = CloudClient(self.server.config(timeout=0.05,max_concurrency=1))
        await client._slots.acquire()
        self.service = CloudCognition(self.store, client)
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await self.infer()
            self.assertEqual(len(self.server.calls), 0)
        finally:
            client._slots.release()

    async def test_stream_is_collected_then_validated(self):
        content = canonical(output())
        chunks = [dict(choices=[dict(delta={'content':content[:30]},finish_reason=None)]),
                  dict(choices=[dict(delta={'content':content[30:]},finish_reason='stop')]),
                  dict(choices=[],usage={'total_tokens': 50})]
        self.server.raw = ''.join('data: '+canonical(chunk)+'\n\n' for chunk in chunks).encode()+b'data: [DONE]\n\n'
        self.service = CloudCognition(self.store, CloudClient(self.server.config(stream=True)))
        self.assertEqual((await self.infer())['data'], plan())
        self.server.raw = b'data: {"choices":[{"delta":{"content":"{"},"finish_reason":null}]}\n\n'
        with self.assertRaises(CloudError):
            await self.infer('partial')

    async def test_clarification_is_not_a_plan(self):
        self.server.response = completion(dict(status='needs_clarification',data=None,questions=['Where is the cup?']))
        self.assertEqual((await self.infer())['status'], 'needs_clarification')
        self.assertEqual(self.journal.tasks(), [])

    async def test_every_model_purpose_uses_same_http_boundary(self):
        predicate = dict(entity='cup',attribute='found',value=True)
        Learning(self.store).experience('episode1','session1',['e'],success=True)
        cases = {
            'plan':plan(), 'perception':dict(observations=[dict(text='a cup',evidence=['e'],uncertainty='uncertain')],missing=[]),
            'memory_extract':dict(claims=[dict(entity='cup',attribute='color',value='red',evidence=['e'],uncertainty='reported')]),
            'summarize':dict(summary='cup reported',evidence=['e'],unresolved=[]),
            'scene':dict(location='unknown',entities=[],uncertainties=['location missing']),
            'goal':dict(description='find cup',predicate=predicate,evidence=['e']),
            'motivation':dict(candidates=[]), 'role':dict(candidates=[]),
            'learning':dict(plan=plan(),training=['episode1'],counterexamples=[],scope='text only'),
            'conflict':dict(assessment='uncertain',evidence=['e'],questions=['same entity?']),
            'association':dict(hypotheses=[dict(question='Where is the cup?',evidence=['e'])]),
            'appraisal':dict(stimulus='calm',evidence=['e']), 'expression':dict(text='Please confirm.',evidence=['e'])}
        for purpose, data in cases.items():
            self.server.response = completion(output(data))
            result = await self.service.infer(purpose,'purpose-'+purpose,{'goal':'g','training':['episode1']})
            self.assertEqual(result['data'], data)
        self.assertEqual(len(self.server.calls), len(SCHEMAS))
        self.assertEqual(self.store.read('knowledge')[0].get('claims', {}), {})

    async def test_cloud_plan_runs_only_through_explicit_skill_operation(self):
        driver = SimulatedDriver(duration=0)
        runtime = Runtime(self.journal, driver)
        await runtime.start()
        try:
            system = CognitiveSystem(self.journal, runtime)
            system._cloud = self.service
            await system.handle(dict(op='model', at=0, request_id='r', input={'goal':'g'}))
            self.assertEqual(driver.execution_order, [])
            result = await system.handle(dict(op='run_cloud_plan',at=0,request_id='r',skill='cloud-plan',version=1,run_id='run1'))
            self.assertEqual(result['status'], 'steps_completed')
            self.assertEqual(len(driver.execution_order), 1)
            self.assertNotEqual(self.goals.evaluate(0)['g']['status'], 'satisfied')
        finally:
            await runtime.close()
            driver.close()

    async def test_superseded_generation_cannot_be_adopted(self):
        await self.infer('old', generation=0)
        await self.infer('new', generation=1)
        driver = SimulatedDriver(duration=0)
        runtime = Runtime(self.journal, driver)
        await runtime.start()
        try:
            system = CognitiveSystem(self.journal, runtime)
            with self.assertRaises(InvalidInput):
                await system.handle(dict(op='run_cloud_plan',at=0,request_id='old',skill='s',version=1,run_id='r'))
            self.assertEqual(driver.execution_order, [])
        finally:
            await runtime.close()
            driver.close()

    async def test_image_audio_and_frame_inputs_use_cloud_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory)/'frame.png'
            audio = Path(directory)/'clip.wav'
            image.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            audio.write_bytes(b'RIFF'+b'\x00'*4+b'WAVEfixture')
            self.service = CloudCognition(self.store, CloudClient(self.server.config(modalities=('text','image','audio'))))
            self.server.response = completion(output(dict(observations=[dict(text='uncertain media',evidence=['frame','audio'],uncertainty='uncertain')],missing=[])))
            result = await self.service.infer('perception','media',{},attachments=[
                dict(id='frame',path=str(image),type='image',at=0), dict(id='audio',path=str(audio),type='audio')])
            self.assertEqual(result['status'], 'ok')
            contents = self.server.calls[0]['body']['messages'][1]['content']
            self.assertEqual([c['type'] for c in contents], ['text','text','image_url','text','input_audio'])
            self.assertTrue(contents[-1]['input_audio']['data'].startswith('data:audio/wav;base64,'))
            ledger = canonical(self.store.read('cloud_requests')[0])
            self.assertNotIn('base64,', ledger)
            self.assertNotIn(directory, ledger)

    async def test_purpose_model_routing_and_json_mode_switch(self):
        self.service = CloudCognition(self.store, CloudClient(self.server.config(models={'plan':'planning-model'}, json_mode=False,
                                                                                 extra_body={'modalities':['text']})))
        await self.infer()
        body = self.server.calls[0]['body']
        self.assertEqual(body['model'], 'planning-model')
        self.assertNotIn('response_format', body)
        self.assertEqual(body['modalities'], ['text'])

    async def test_pending_request_is_not_reissued_after_restart(self):
        await self.infer()
        with self.store.edit('cloud_requests','crash') as state:
            state['requests']['r']['status'] = 'pending'
        restarted = CloudCognition(StateStore(self.journal), self.client)
        with self.assertRaises(InvalidInput):
            await restarted.infer('plan','r',{'goal':'g'})
        self.assertEqual(len(self.server.calls), 1)

    async def test_plan_cannot_exceed_remaining_action_budget(self):
        self.goals.add('limited', dict(entity='robot',attribute='checked',value=True),budget=1)
        candidate = plan()
        candidate['goal'] = 'limited'
        candidate['steps'].append(dict(id='s2',capability='say',arguments={'text':'again'},after=['s1']))
        self.server.response = completion(output(candidate))
        with self.assertRaises(InvalidInput):
            await self.service.infer('plan','limited-request',{'goal':'limited'})
        self.assertEqual(self.journal.tasks(), [])
