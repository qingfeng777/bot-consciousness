"""Daily schedule restart/DST semantics and fail-closed skill conditions."""
from datetime import datetime, timezone
import unittest

from consciousness.agency import Goals
from consciousness.calendar import Calendar
from consciousness.domain import InvalidInput
from consciousness.journal import Journal
from consciousness.knowledge import Knowledge
from consciousness.planning import Skills, validate_plan
from consciousness.state import StateStore
import test_cognition as fixtures

plan = fixtures.plan


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.addCleanup(self.journal.close)
        self.store = StateStore(self.journal)
        self.knowledge = Knowledge(self.store)
        self.goals = Goals(self.store, self.knowledge)
        self.calendar = Calendar(self.store, self.goals)
        self.predicate = dict(entity='robot', attribute='checked', value=True)

    def test_restart_and_occurrence_idempotency(self):
        self.calendar.configure('daily', self.predicate, hour=8, minute=0, start=utc('2024-01-01'))
        first = self.calendar.tick(utc('2024-01-01T08:00:00'))
        self.assertEqual(len(first), 1)
        restarted = Calendar(StateStore(self.journal), self.goals)
        self.assertEqual(restarted.tick(utc('2024-01-01T09:00:00')), [])
        second = restarted.tick(utc('2024-01-02T08:00:00'))
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)
        goals = self.store.read('goals')[0]['items']
        self.assertNotEqual(goals[first[0]]['definition']['predicate']['entity'], goals[second[0]]['definition']['predicate']['entity'])

    def test_catchup_policies_and_clock_rollback(self):
        for policy in ['skip', 'run_once', 'bounded_catchup']:
            self.calendar.configure(policy, self.predicate, hour=8, minute=0,
                                    start=utc('2024-01-01'), policy=policy, catchup=3)
        created = self.calendar.tick(utc('2024-01-06T09:00:00'))
        self.assertEqual(len(created), 4)
        self.assertEqual(self.calendar.tick(utc('2024-01-05T09:00:00')), [])
        self.assertEqual(self.calendar.tick(utc('2024-01-06T09:00:00')), [])

    def test_skip_allows_on_time_tick(self):
        self.calendar.configure('skip', self.predicate, hour=8, minute=0,
                                start=utc('2024-01-01'), policy='skip')
        self.assertEqual(len(self.calendar.tick(utc('2024-01-01T08:00:30'))), 1)

    def test_dst_gap_is_not_silently_shifted(self):
        self.calendar.configure('gap', self.predicate, hour=2, minute=30, zone='America/New_York',
                                start=utc('2024-03-10T00:00:00'))
        self.assertEqual(self.calendar.tick(utc('2024-03-10T12:00:00')), [])
        self.assertEqual(len(self.calendar.tick(utc('2024-03-11T12:00:00'))), 1)

    def test_dst_fold_explicitly_selects_one_occurrence(self):
        for fold in [0, 1]:
            self.calendar.configure('fold' + str(fold), self.predicate, hour=1, minute=30,
                                    zone='America/New_York', start=utc('2024-11-03T00:00:00'), fold=fold)
        self.assertEqual(len(self.calendar.tick(utc('2024-11-03T05:45:00'))), 1)
        self.assertEqual(len(self.calendar.tick(utc('2024-11-03T06:45:00'))), 1)
        due = sorted(x['due'] for x in self.store.read('calendar')[0]['occurrences'].values())
        self.assertEqual(due[1] - due[0], 3600)

    def test_crash_between_goal_and_calendar_checkpoint_is_idempotent(self):
        self.calendar.configure('daily', self.predicate, hour=8, minute=0, start=utc('2024-01-01'))
        first = self.calendar.tick(utc('2024-01-01T08:00:00'))
        with self.store.edit('calendar', 'crash') as state:
            state['rules']['daily']['through'] = None
        self.assertEqual(self.calendar.tick(utc('2024-01-01T08:00:00')), first)
        self.assertEqual(len(self.store.read('goals')[0]['items']), 1)

    def test_invalid_zone_and_disabled_rule(self):
        with self.assertRaises(InvalidInput):
            self.calendar.configure('invalid', self.predicate, hour=1, minute=0, zone='not/a-zone')
        self.calendar.configure('disabled', self.predicate, hour=8, minute=0, start=utc('2024-01-01'))
        self.calendar.disable('disabled')
        self.assertEqual(self.calendar.tick(utc('2024-01-02')), [])


# Reuse fixtures without inheriting the base class's test methods.
class SkillConditionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CognitiveAsyncTests.asyncSetUp
    asyncTearDown = fixtures.CognitiveAsyncTests.asyncTearDown

    async def test_unknown_and_disputed_preconditions_block(self):
        skills = Skills(self.store, self.goals, self.knowledge)
        candidate = plan()
        candidate['requires'] = [dict(entity='cup', attribute='present', value=True)]
        key = skills.register('conditional', 1, candidate)
        self.assertEqual((await skills.run(key, 'r', self.runtime, at=0))['status'], 'blocked')
        self.knowledge.claim('a', 'cup', 'present', True, ['e'])
        self.knowledge.claim('b', 'cup', 'present', False, ['e'])
        self.assertEqual((await skills.run(key, 'r', self.runtime, at=0))['status'], 'blocked')
        self.knowledge.resolve('1', 'a', ['e'], 'verified')
        self.assertEqual((await skills.run(key, 'r', self.runtime, at=0))['status'], 'steps_completed')
        self.assertEqual(len(self.driver.execution_order), 1)

    async def test_false_branch_skips_without_consuming_budget(self):
        skills = Skills(self.store, self.goals, self.knowledge)
        self.knowledge.claim('a', 'cup', 'present', False, ['e'])
        candidate = plan()
        candidate['steps'][0]['when'] = dict(entity='cup', attribute='present', value=True)
        key = skills.register('conditional', 1, candidate)
        result = await skills.run(key, 'r', self.runtime, at=0)
        self.assertEqual(result['skipped'], ['s1'])
        self.assertEqual(self.driver.execution_order, [])
        self.assertEqual(self.goals.evaluate(0)['g']['attempts'], [])

    async def test_conditions_rechecked_between_steps(self):
        skills = Skills(self.store, self.goals, self.knowledge)
        self.goals.add('two', dict(entity='robot', attribute='two_done', value=True), budget=2)
        self.knowledge.evidence('condition', 'sensor', 'cup is present')
        self.knowledge.claim('a', 'cup', 'present', True, ['condition'])
        candidate = plan('two')
        candidate['requires'] = [dict(entity='cup', attribute='present', value=True)]
        candidate['steps'].append(dict(id='s2', capability='say', arguments={'text': 'second'}, after=['s1']))
        key = skills.register('two', 1, candidate)
        original_execute = self.driver.execute
        async def execute(action):
            result = await original_execute(action)
            self.knowledge.withdraw('condition')
            return result
        self.driver.execute = execute
        result = await skills.run(key, 'r', self.runtime, at=0)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['completed'], ['s1'])
        self.assertEqual(len(self.driver.execution_order), 1)

    async def test_bad_condition_language_rejected(self):
        candidate = plan()
        candidate['requires'] = [{'code': 'run arbitrary code'}]
        with self.assertRaises(InvalidInput):
            validate_plan(candidate)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.addCleanup(self.journal.close)
        self.store = StateStore(self.journal)
        self.knowledge = Knowledge(self.store)
        self.knowledge.evidence('e', 'sensor', 'observed')

    def test_fts_ranking_filters_time_and_marks_stale(self):
        k = self.knowledge
        k.claim('a', 'cup', 'location', 'wooden desk', ['e'], start=0, end=10)
        k.claim('b', 'cup', 'location', 'metal shelf', ['e'], start=10)
        self.assertTrue(k.ranked('wooden', at=0)[0]['stale_index'])
        k.rebuild_index()
        hit = k.ranked('wooden', at=0)[0]
        self.assertEqual(hit['retrieval'], 'fts5_bm25')
        self.assertFalse(hit['stale_index'])
        self.assertEqual(k.ranked('wooden', at=10), [])
        k.withdraw('e')
        self.assertEqual(k.ranked('wooden', at=0), [])

    def test_fts_does_not_hide_conflicts_or_ignore_resolution(self):
        k = self.knowledge
        k.claim('a', 'cup', 'location', 'wooden desk', ['e'])
        k.claim('b', 'cup', 'location', 'wooden shelf', ['e'])
        k.rebuild_index()
        self.assertTrue(all(not h['certain'] for h in k.ranked('wooden')))
        k.resolve('1', 'b', ['e'], 'checked')
        self.assertTrue(k.ranked('wooden')[0]['stale_index'])
        k.rebuild_index()
        self.assertEqual([h['id'] for h in k.ranked('wooden')], ['b'])

    def test_fts_rebuild_failure_preserves_facts(self):
        k = self.knowledge
        k.claim('a', 'cup', 'location', 'wooden desk', ['e'])
        self.journal._db.execute('CREATE TABLE memory_fts (wrong_column TEXT)')
        self.journal._db.commit()
        with self.assertRaises(InvalidInput):
            k.rebuild_index()
        self.assertEqual(k.search(query='wooden')[0]['id'], 'a')
        self.assertTrue(k.ranked('wooden')[0]['stale_index'])
