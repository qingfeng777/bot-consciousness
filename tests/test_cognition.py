"""Contract and failure-path tests for bounded cognitive simulation modules."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from consciousness.agency import Affect, Goals, Identity, Motivation, Scene, SelfState
from consciousness.domain import DuplicateConflict, InvalidInput, RevisionConflict, canonical
from consciousness.driver import SimulatedDriver
from consciousness.journal import Journal
from consciousness.knowledge import Knowledge
from consciousness.learning import Association, Learning, Reflexes
from consciousness.perception import Perception
from consciousness.planning import ModelGateway, Skills, validate_plan
from consciousness.runtime import Runtime
from consciousness.simulation import simulate
from consciousness.state import StateStore


def plan(goal='g'):
    return dict(goal=goal, evidence=['e'], steps=[dict(id='s1', capability='say', arguments={'text': 'checking'}, after=[])])


class CognitiveTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.addCleanup(self.journal.close)
        self.store = StateStore(self.journal)
        self.knowledge = Knowledge(self.store)
        self.knowledge.evidence('e', 'sensor', 'observation')

    def test_transaction_rollback_and_revision_guard(self):
        with self.assertRaises(RuntimeError):
            with self.store.edit('x', 'cause') as state:
                state['value'] = 1
                raise RuntimeError('fail')
        self.assertEqual(self.store.read('x'), ({}, 0))
        with self.store.edit('x', 'cause', expected=0) as state:
            state['value'] = 2
        with self.assertRaises(RevisionConflict):
            with self.store.edit('x', 'cause', expected=0):
                pass
        self.assertEqual(self.store.read('x'), ({'value': 2}, 1))

    def test_temporal_conflicts_units_and_context(self):
        k = self.knowledge
        k.claim('a', 'cup', 'height', 100, ['e'], start=0, end=10, unit='cm')
        k.claim('b', 'cup', 'height', 1, ['e'], start=0, end=10, unit='m')
        k.claim('c', 'cup', 'height', 2, ['e'], start=10, unit='m')
        k.claim('d', 'cup', 'height', 3, ['e'], start=0, unit='m', context='other')
        self.assertEqual(k.conflicts(), [])
        k.claim('z', 'cup', 'height', 2, ['e'], start=5, end=10, unit='m')
        self.assertEqual(len(k.conflicts()), 2)
        self.assertTrue(all(not h['certain'] for h in k.search(entity='cup', at=6)))
        self.assertEqual([h['id'] for h in k.search(at=10)], ['c'])

    def test_withdrawal_propagates_independent_of_id_sorting(self):
        k = self.knowledge
        k.claim('z', 'cup', 'color', 'red', ['e'])
        k.claim('b', 'cup', 'visible', True, ['e'], parents=['z'])
        k.claim('a', 'cup', 'usable', True, ['e'], parents=['b'])
        k.withdraw('e')
        self.assertEqual(k.search(at=0), [])
        self.assertTrue(all(c['status'] == 'needs_verification' for c in self.store.read('knowledge')[0]['claims'].values()))

    def test_resolution_reopens_and_invalidates_derived(self):
        k = self.knowledge
        for eid in ['e2', 'decision']:
            k.evidence(eid, 'sensor', eid)
        k.claim('z', 'cup', 'color', 'red', ['e'])
        k.claim('a', 'cup', 'color', 'blue', ['e2'])
        k.resolve('1', 'z', ['decision'], 'independent check')
        self.assertEqual([h['value'] for h in k.search()], ['red'])
        k.claim('derived', 'cup', 'selected', True, ['e'], parents=['z'])
        k.withdraw('decision')
        self.assertEqual(k.conflicts()[0]['status'], 'reopened')
        self.assertEqual(self.store.read('knowledge')[0]['claims']['derived']['status'], 'needs_verification')
        self.assertTrue(all(not h['certain'] for h in k.search()))

    def test_evidence_and_claim_idempotency(self):
        k = self.knowledge
        k.claim('a', 'cup', 'color', 'red', ['e'])
        revision = self.store.read('knowledge')[1]
        k.claim('a', 'cup', 'color', 'red', ['e'])
        self.assertEqual(self.store.read('knowledge')[1], revision)
        with self.assertRaises(DuplicateConflict):
            k.claim('a', 'cup', 'color', 'blue', ['e'])
        with self.assertRaises(InvalidInput):
            k.claim('b', 'cup', 'color', 'red', ['missing'])
        k.withdraw('e')
        k.evidence('e', 'sensor', 'observation')
        self.assertFalse(self.store.read('knowledge')[0]['evidence']['e']['active'])

    def test_resolution_only_applies_in_overlapping_time(self):
        k = self.knowledge
        k.claim('a', 'cup', 'location', 'desk', ['e'], start=0, end=20)
        k.claim('b', 'cup', 'location', 'shelf', ['e'], start=5, end=10)
        self.assertTrue(k.search(at=1)[0]['certain'])
        k.resolve('1', 'b', ['e'], 'checked overlapping interval')
        self.assertEqual([h['value'] for h in k.search(at=1)], ['desk'])
        self.assertEqual([h['value'] for h in k.search(at=7)], ['shelf'])
        self.assertEqual([h['value'] for h in k.search(at=15)], ['desk'])

    def test_retracted_premise_invalidates_chain_with_separate_sources(self):
        k = self.knowledge
        k.evidence('independent', 'sensor', 'independent observation')
        k.claim('z', 'cup', 'exists', True, ['e'])
        k.claim('b', 'cup', 'usable', True, ['independent'], parents=['z'])
        k.claim('a', 'cup', 'selected', True, ['independent'], parents=['b'])
        k.retract('z')
        self.assertEqual(k.search(), [])

    def test_backup_restores_module_state(self):
        self.knowledge.claim('a', 'cup', 'color', 'red', ['e'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.sqlite3'
            self.journal.backup(path)
            with_journal = Journal(path)
            try:
                restored = Knowledge(StateStore(with_journal))
                self.assertEqual(restored.search(), self.knowledge.search())
            finally:
                with_journal.close()

    def test_derived_validity_cannot_outlive_premise(self):
        self.knowledge.claim('a', 'cup', 'present', True, ['e'], start=5, end=10)
        with self.assertRaises(InvalidInput):
            self.knowledge.claim('b', 'cup', 'usable', True, ['e'], start=5, parents=['a'])

    def test_scene_separates_names_and_keeps_late_history(self):
        scene = Scene(self.store)
        scene.observe('a', location='room', sensor='cam1', track='1', label='Alex', at=10)
        scene.observe('b', location='room', sensor='cam2', track='1', label='Alex', at=11)
        scene.observe('c', location='past', sensor='cam1', track='1', label='Alex', at=0)
        self.assertEqual(len(scene.snapshot()['entities']), 2)
        self.assertEqual(scene.snapshot()['location'], 'room')
        self.assertEqual(scene.snapshot()['historical_corrections'], ['c'])
        scene.observe('d', location='hall', sensor='cam1', track='1', label='Alex', at=12)
        self.assertEqual(len(scene.snapshot()['episodes']), 2)

    def test_telemetry_ttl_quality_units_and_hysteresis(self):
        telemetry = SelfState(self.store)
        telemetry.sample('a', 'battery', 19, '%', 0, ttl=10)
        telemetry.sample('b', 'battery', 25, '%', 1, ttl=10)
        self.assertTrue(telemetry.snapshot(1)['low_battery'])
        self.assertEqual(telemetry.snapshot(11)['battery']['validity'], 'unknown')
        telemetry.sample('c', 'battery', 31, '%', 12, quality=0.4)
        self.assertTrue(telemetry.snapshot(12)['low_battery'])
        self.assertEqual(telemetry.snapshot(12)['battery']['validity'], 'degraded')
        telemetry.sample('d', 'battery', 31, '%', 13)
        self.assertFalse(telemetry.snapshot(13)['low_battery'])
        for value, unit in [(float('nan'), '%'), (101, '%'), (50, 'volt')]:
            with self.assertRaises(InvalidInput):
                telemetry.sample('invalid', 'battery', value, unit, 14)

    def test_goal_requires_observable_evidence_and_reopens(self):
        goals = Goals(self.store, self.knowledge)
        goals.add('z', dict(entity='cup', attribute='found', value=True), budget=1)
        goals.add('a', dict(entity='robot', attribute='ready', value=True), dependencies=['z'])
        self.assertEqual(goals.evaluate(0)['a']['status'], 'blocked')
        goals.reserve('z', 'step1')
        goals.reserve('z', 'step1')
        self.assertEqual(goals.evaluate(0)['z']['status'], 'blocked')
        self.knowledge.claim('found', 'cup', 'found', True, ['e'])
        self.assertEqual(goals.evaluate(1)['z']['status'], 'satisfied')
        self.assertEqual(goals.evaluate(1)['a']['status'], 'active')
        self.knowledge.withdraw('e')
        self.assertEqual(goals.evaluate(2)['z']['status'], 'blocked')
        self.assertEqual(goals.evaluate(2)['a']['status'], 'blocked')
        with self.assertRaises(InvalidInput):
            goals.add('cycle', dict(entity='x', attribute='a', value=1), dependencies=['cycle'])

    def test_motivation_deduplicates_low_battery(self):
        goals = Goals(self.store, self.knowledge)
        telemetry = SelfState(self.store)
        motives = Motivation(self.store, goals, telemetry)
        self.assertEqual(motives.tick(0)['status'], 'unknown')
        telemetry.sample('a', 'battery', 10, '%', 0)
        first = motives.tick(0)
        telemetry.sample('b', 'battery', 15, '%', 1)
        self.assertEqual(motives.tick(1), first)
        self.assertEqual(len(self.store.read('goals')[0]['items']), 1)

    def test_new_charge_cycle_cannot_reuse_old_completion(self):
        goals = Goals(self.store, self.knowledge)
        telemetry = SelfState(self.store)
        motives = Motivation(self.store, goals, telemetry)
        telemetry.sample('a', 'battery', 10, '%', 0)
        first = motives.tick(0)['goal']
        telemetry.sample('b', 'battery', 40, '%', 1)
        motives.tick(1)
        self.assertEqual(goals.evaluate(1)[first]['status'], 'satisfied')
        telemetry.sample('c', 'battery', 10, '%', 2)
        second = motives.tick(2)['goal']
        self.assertNotEqual(first, second)
        self.assertEqual(goals.evaluate(2)[second]['status'], 'active')

    def test_affect_decay_dedup_and_revoke(self):
        affect = Affect(self.store)
        affect.apply('a', 'emergency', 0)
        first = affect.snapshot(0)
        affect.apply('a', 'emergency', 0)
        self.assertEqual(affect.snapshot(0), first)
        self.assertEqual(first['label'], 'alert')
        self.assertEqual(affect.snapshot(1000)['label'], 'calm')
        affect.revoke('a')
        self.assertEqual(affect.snapshot(1)['arousal'], 0)

    def test_roles_versioned_and_expiring(self):
        identity = Identity(self.store)
        identity.configure('helper', 'friendly', version=1, expires=5)
        self.assertEqual(identity.snapshot(4)['style'], 'friendly')
        self.assertEqual(identity.snapshot(5)['style'], 'neutral')
        with self.assertRaises(DuplicateConflict):
            identity.configure('helper', 'concise', version=1)

    def test_perception_provenance_quality_and_partial(self):
        perception = Perception(self.store, self.knowledge)
        for modality in ('image', 'audio', 'video'):
            result = perception.ingest(modality, modality, b'fixture', at=0, annotation='cup')
            self.assertTrue(result['simulation'])
        self.assertEqual(perception.ingest('blur', 'image', b'bad', at=0, quality=0.2, annotation='cup')['status'], 'unknown')
        self.assertEqual(perception.ingest('partial', 'audio', b'part', at=0, partial=True, annotation='cup')['status'], 'partial')
        self.assertNotIn('partial', self.store.read('knowledge')[0]['evidence'])
        with self.assertRaises(InvalidInput):
            perception.ingest('bad', 'text', b'\xff', at=0)

    def test_plan_schema_rejects_code_cycles_and_budget(self):
        candidate = plan()
        validate_plan(candidate)
        for bad in [dict(candidate, extra='x'), dict(candidate, steps=[]), dict(candidate, evidence=[])]:
            with self.assertRaises(InvalidInput):
                validate_plan(bad)
        candidate['steps'][0]['capability'] = 'shell'
        with self.assertRaises(InvalidInput):
            validate_plan(candidate)
        candidate = plan()
        candidate['steps'][0]['after'] = ['s1']
        with self.assertRaises(InvalidInput):
            validate_plan(candidate)

    def test_learning_independent_sessions_and_publication_gate(self):
        learning = Learning(self.store)
        goals = Goals(self.store, self.knowledge)
        skills = Skills(self.store, goals, self.knowledge)
        for i in range(4):
            self.knowledge.evidence('e' + str(i), 'sensor', str(i), group='group' + str(i))
            learning.experience('x' + str(i), 'session' + str(i), ['e' + str(i)], success=i != 3)
        learning.propose('candidate', plan(), ['x0'])
        with self.assertRaises(InvalidInput):
            learning.evaluate('candidate', ['x0', 'x1'], [True, True], [True, False])
        result = learning.evaluate('candidate', ['x1', 'x2'], [True, True], [True, False])
        self.assertTrue(result['passed'])
        with self.assertRaises(InvalidInput):
            learning.publish('candidate', skills)
        key = learning.publish('candidate', skills, configured=True)
        self.assertIn(key, self.store.read('skills')[0]['definitions'])
        learning.retire('candidate', skills)
        self.assertTrue(self.store.read('skills')[0]['retired'][key])
        self.assertFalse(self.store.read('learning')[0]['experiences']['x3']['success'])

    def test_association_bounded_and_separate_from_facts(self):
        association = Association(self.store)
        first = association.propose('e', ['where is the cup?'], generation=1)
        self.assertEqual(first, association.propose('e', ['where is the cup?'], generation=1))
        self.assertEqual(self.knowledge.search(), [])
        with self.assertRaises(InvalidInput):
            association.propose(first[0]['id'], ['next?'], generation=1)
        with self.assertRaises(InvalidInput):
            association.propose('e', ['q'] * 6, generation=1)
        with self.assertRaises(InvalidInput):
            association.propose('e', ['q'], generation=0)

    def test_learning_detects_shared_source_despite_session_names(self):
        learning = Learning(self.store)
        self.knowledge.evidence('copy', 'model', 'rephrased observation', group='e')
        learning.experience('train', 'one', ['e'], success=True)
        learning.experience('test', 'two', ['copy'], success=True)
        learning.propose('c', plan(), ['train'])
        with self.assertRaises(InvalidInput):
            learning.evaluate('c', ['test'], [True], [False])


class CognitiveAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.journal = Journal()
        self.store = StateStore(self.journal)
        self.knowledge = Knowledge(self.store)
        self.knowledge.evidence('e', 'sensor', 'observed')
        self.goals = Goals(self.store, self.knowledge)
        self.goals.add('g', dict(entity='robot', attribute='done', value=True), budget=1)
        self.driver = SimulatedDriver(duration=0)
        self.runtime = Runtime(self.journal, self.driver)
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.close()
        self.driver.close()
        self.journal.close()

    async def test_skill_recovery_does_not_repeat_effect(self):
        skills = Skills(self.store, self.goals, self.knowledge)
        key = skills.register('demo', 1, plan())
        first = await skills.run(key, 'r', self.runtime, at=0)
        self.assertEqual(first['status'], 'steps_completed')
        self.assertNotEqual(self.goals.evaluate(0)['g']['status'], 'satisfied')
        # Simulate crash window after receipt, before checkpoint commit.
        with self.store.edit('skills', 'crash') as state:
            state['runs']['r']['completed'] = []
        second = await skills.run(key, 'r', self.runtime, at=0)
        self.assertEqual(second['status'], 'steps_completed')
        self.assertEqual(len(self.driver.execution_order), 1)
        self.assertEqual(self.driver.max_active, 1)

    async def test_skill_withdrawn_evidence_blocks_before_effect(self):
        skills = Skills(self.store, self.goals, self.knowledge)
        key = skills.register('demo', 1, plan())
        self.knowledge.withdraw('e')
        self.assertEqual((await skills.run(key, 'r', self.runtime, at=0))['status'], 'blocked')
        self.assertEqual(self.driver.execution_order, [])

    async def test_reserved_budget_recovers_before_task_admission(self):
        skills = Skills(self.store, self.goals, self.knowledge)
        key = skills.register('demo', 1, plan())
        self.goals.evaluate(0)
        event = 'step-' + hashlib.sha256(canonical(['r', key, 's1']).encode()).hexdigest()
        self.goals.reserve('g', event)
        result = await skills.run(key, 'r', self.runtime, at=0)
        self.assertEqual(result['status'], 'steps_completed')
        self.assertEqual(len(self.goals.evaluate(0)['g']['attempts']), 1)

    async def test_skill_provenance_revocation_blocks_published_version(self):
        self.knowledge.evidence('training', 'sensor', 'training evidence')
        skills = Skills(self.store, self.goals, self.knowledge)
        key = skills.register('demo', 1, plan(), provenance=['training'])
        self.knowledge.withdraw('training')
        self.assertEqual((await skills.run(key, 'r', self.runtime, at=0))['status'], 'blocked')
        self.assertEqual(self.driver.execution_order, [])

    async def test_retired_skill_and_changed_run_version_rejected(self):
        skills = Skills(self.store, self.goals, self.knowledge)
        key = skills.register('demo', 1, plan())
        await skills.run(key, 'r', self.runtime, at=0)
        key2 = skills.register('demo', 2, plan())
        with self.assertRaises(DuplicateConflict):
            await skills.run(key2, 'r', self.runtime, at=0)
        skills.retire(key)
        with self.assertRaises(InvalidInput):
            await skills.run(key, 'another', self.runtime, at=0)

    async def test_command_dedup_binds_capability(self):
        await self.runtime.submit_action('say', {'text': 'hello'}, event_id='command')
        with self.assertRaises(DuplicateConflict):
            await self.runtime.submit_action('alert', {'text': 'hello'}, event_id='command')
        await self.runtime.drain()

    async def test_gateway_timeout_budget_and_bad_json(self):
        async def slow(context):
            await asyncio.sleep(0.1)
        gateway = ModelGateway(self.store, slow, max_calls=1, timeout=0.001)
        with self.assertRaises(asyncio.TimeoutError):
            await gateway.generate('g', {}, 0)
        with self.assertRaises(InvalidInput):
            await gateway.generate('g', {}, 0)
        async def bad(context):
            return '{'
        gateway = ModelGateway(self.store, bad)
        with self.assertRaises(ValueError):
            await gateway.generate('other', {}, 0)

    async def test_gateway_rejects_stale_response(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def provider(context):
            if context.get('slow'):
                started.set()
                await release.wait()
            return canonical(plan())
        gateway = ModelGateway(self.store, provider)
        task = asyncio.create_task(gateway.generate('g', {'slow': True}, 0))
        await started.wait()
        await gateway.generate('g', {}, 1)
        release.set()
        with self.assertRaises(InvalidInput):
            await task

    async def test_gateway_rejects_fabricated_evidence(self):
        async def provider(context):
            return canonical(dict(plan(), evidence=['invented']))
        gateway = ModelGateway(self.store, provider)
        with self.assertRaises(InvalidInput):
            await gateway.generate('g', {}, 0)

    async def test_reflex_cooldown_entity_isolation_and_conflict(self):
        reflex = Reflexes(self.store)
        reflex.configure('fire', 'fire', 'alarm', cooldown=5)
        a = await reflex.react('a', 'fire', 'room1', 0, self.runtime)
        await self.runtime.drain()
        self.assertEqual(a['status'], 'dispatched')
        self.assertEqual((await reflex.react('b', 'fire', 'room1', 1, self.runtime))['status'], 'suppressed')
        self.assertEqual((await reflex.react('c', 'fire', 'room2', 1, self.runtime))['status'], 'dispatched')
        await self.runtime.drain()
        reflex.configure('conflicting', 'fire', 'another alarm')
        self.assertEqual((await reflex.react('d', 'fire', 'room1', 6, self.runtime))['status'], 'conflict')
        self.assertEqual(len(self.driver.execution_order), 2)

    async def test_integrated_scenario_restart_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await simulate('examples/cognitive.jsonl', directory)
            self.assertEqual(result['goals']['g']['status'], 'satisfied')
            self.assertLessEqual(result['max_external_concurrency'], 1)
            again = await simulate('examples/cognitive.jsonl', directory)
            self.assertEqual(result['runtime']['counts'], again['runtime']['counts'])
            self.assertEqual(again['max_external_concurrency'], 0)
            self.assertEqual(result['facts'], again['facts'])
