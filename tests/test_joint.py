"""Contract tests for joint review tasks (JOINT_REVIEW.md)."""
from dataclasses import replace
from fractions import Fraction

from review.close.history import read
from review.close.publish import finalize
from review.field.acceptance import receive
from review.field.handover import read_dispatch_outcome
from review.field.joint import JointManifest, joint_permit
from review.handoff.delivery import DeliveryChannel
from review.handoff.retry import deliver_joint, retry_dispatch, sync_dispatch
from review.pick.case import CaseStore, open as open_case
from review.pick.dispatch import DispatchLedger
from review.pick.joint import JointTaskStore, open_joint, solve_allocation
from review.pick.replan import correct
from review.storage import connect
from tests.scenario import ProjectTest


class JointFixture(ProjectTest):
    def record_joint(self):
        return self.catalog.record(
            'joint-coating', None, [('A', 10), ('B', 9), ('C', 8), ('D', 7)])

    def open_full(self, task_id='J-full'):
        revision = self.record_joint()
        result = open_joint(self.analysis, self.channel, task_id, revision.revision_id,
                            [('甲', ['D', 'C', 'B', 'A']), ('乙', ['B', 'A'])])
        return revision, result

    def assignments_by_specimen(self, result):
        return {item.envelope.specimen_id: item.envelope for item in result['assignments']}


class JointOpeningTests(JointFixture):
    def test_full_request_fixes_exclusive_ownership(self):
        revision, result = self.open_full()
        self.assertEqual(result['status'], 'ready')
        assignments = result['assignments']
        self.assertEqual([(item.envelope.case_id, item.envelope.specimen_id, item.slot) for item in assignments],
                         [('甲', 'C', 1), ('甲', 'D', 2), ('乙', 'A', 1), ('乙', 'B', 2)])
        specimens = [item.envelope.specimen_id for item in assignments]
        self.assertEqual(len(specimens), len(set(specimens)))
        for item in assignments:
            self.assertEqual(item.envelope.revision_id, revision.revision_id)
        self.assertEqual(CaseStore(self.analysis).require('甲').effective_revision, revision.revision_id)
        manifest = JointManifest(self.field).lookup_task('J-full')
        self.assertEqual([row['dispatch_id'] for row in manifest],
                         [item.envelope.dispatch_id for item in assignments])

    def test_shortage_returns_witness_and_creates_nothing(self):
        revision = self.record_joint()
        result = open_joint(self.analysis, self.channel, 'J-short', revision.revision_id,
                            [('甲', ['A', 'B', 'C']), ('乙', ['A', 'B', 'C'])])
        self.assertEqual(result, {'status': 'shortage', 'cases': ('甲', '乙'),
                                  'candidates': ('A', 'B', 'C')})
        self.assertEqual(JointTaskStore(self.analysis).require_task('J-short').status, 'shortage')
        self.assertIsNone(CaseStore(self.analysis).get('甲'))
        self.assertEqual(DispatchLedger(self.analysis).list_case_dispatches('甲'), ())
        self.assertEqual(JointManifest(self.field).lookup_task('J-short'), ())
        # Repeating the identical request returns the same witness.
        repeated = open_joint(self.analysis, self.channel, 'J-short', revision.revision_id,
                              [('甲', ['A', 'B', 'C']), ('乙', ['A', 'B', 'C'])])
        self.assertEqual(repeated, result)

    def test_shortage_witness_size_constraint(self):
        revision = self.record_joint()
        result = open_joint(self.analysis, self.channel, 'J-single', revision.revision_id,
                            [('孤', ['A'])])
        self.assertEqual(result, {'status': 'shortage', 'cases': ('孤',), 'candidates': ('A',)})

    def test_preference_prefers_lexicographically_smallest_complete_ranking(self):
        revision = self.record_joint()
        result = open_joint(self.analysis, self.channel, 'J-pref', revision.revision_id,
                            [('甲', ['A', 'B', 'C', 'D']), ('乙', ['A', 'B', 'C'])])
        self.assertEqual([(item.envelope.case_id, item.envelope.specimen_id) for item in result['assignments']],
                         [('甲', 'A'), ('甲', 'D'), ('乙', 'B'), ('乙', 'C')])

    def test_solver_rank_tie_breaks_by_specimen_id(self):
        revision = self.catalog.record('tie', None, [('B', '5/2'), ('A', '5/2'), ('C', 2), ('D', 1)])
        result = open_joint(self.analysis, self.channel, 'J-tie', revision.revision_id,
                            [('一', ['A', 'B', 'C', 'D']), ('二', ['C', 'D'])])
        # A and B tie on thickness: specimen id order puts A first inside case one.
        self.assertEqual([(item.envelope.case_id, item.envelope.specimen_id) for item in result['assignments']],
                         [('一', 'A'), ('一', 'B'), ('二', 'C'), ('二', 'D')])
        # Feasibility can force skipping the globally top-ranked specimen.
        forced = open_joint(self.analysis, self.channel, 'J-forced', revision.revision_id,
                            [('三', ['A', 'B', 'C', 'D']), ('四', ['A', 'B'])])
        self.assertEqual([(item.envelope.case_id, item.envelope.specimen_id) for item in forced['assignments']],
                         [('三', 'C'), ('三', 'D'), ('四', 'A'), ('四', 'B')])
        # Direct solver contract: infeasible when both cases only allow A/B.
        self.assertIsNone(solve_allocation((frozenset(('A', 'B')), frozenset(('A', 'B'))), ('A', 'B')))

    def test_validation_rejects_bad_requests(self):
        revision = self.record_joint()
        open_kwargs = dict(analysis=self.analysis, channel=self.channel, task_id='J-bad',
                           revision_id=revision.revision_id)
        with self.assertRaisesRegex(ValueError, 'nonempty'):
            open_joint(cases=[], **open_kwargs)
        with self.assertRaisesRegex(ValueError, 'duplicate joint case'):
            open_joint(cases=[('甲', ['A', 'B']), ('甲', ['C', 'D'])], **open_kwargs)
        with self.assertRaisesRegex(ValueError, 'duplicate joint candidate'):
            open_joint(cases=[('甲', ['A', 'A'])], **open_kwargs)
        with self.assertRaisesRegex(ValueError, 'unknown joint candidate'):
            open_joint(cases=[('甲', ['A', 'Z'])], **open_kwargs)
        with self.assertRaisesRegex(ValueError, 'unknown revision'):
            open_joint(self.analysis, self.channel, 'J-bad', revision.revision_id + 10,
                       [('甲', ['A', 'B'])])
        self.assertIsNone(JointTaskStore(self.analysis).get_task('J-bad'))

    def test_case_id_is_reserved_against_other_openings(self):
        self.open_full()
        other_batch = self.catalog.record('other-coating', None,
                                          [('A', 10), ('B', 9), ('C', 8), ('D', 7)])
        with self.assertRaisesRegex(ValueError, 'case already exists'):
            open_case(self.analysis, '甲', other_batch.revision_id)
        with self.assertRaisesRegex(ValueError, 'case already exists'):
            open_joint(self.analysis, self.channel, 'J-other', other_batch.revision_id,
                       [('甲', ['A', 'B'])])

    def test_same_task_changed_inputs_are_rejected(self):
        revision, _ = self.open_full()
        with self.assertRaisesRegex(ValueError, 'different cases or candidates'):
            open_joint(self.analysis, self.channel, 'J-full', revision.revision_id,
                       [('甲', ['D', 'C', 'B']), ('乙', ['B', 'A'])])
        later = self.catalog.record('joint-coating', revision.revision_id,
                                    [('A', 1), ('B', 2), ('C', 3), ('D', 4)])
        with self.assertRaisesRegex(ValueError, 'different revision'):
            open_joint(self.analysis, self.channel, 'J-full', later.revision_id,
                       [('甲', ['D', 'C', 'B', 'A']), ('乙', ['B', 'A'])])


class JointPendingTests(JointFixture):
    def test_pending_resumes_with_same_assignments_and_dispatch_ids(self):
        revision = self.record_joint()
        offline = DeliveryChannel(None)
        pending = open_joint(self.analysis, offline, 'J-pend', revision.revision_id,
                             [('甲', ['A', 'B']), ('乙', ['C', 'D'])])
        self.assertEqual(pending, {'status': 'pending'})
        self.assertEqual(JointManifest(self.field).lookup_task('J-pend'), ())
        self.assertEqual(JointTaskStore(self.analysis).require_task('J-pend').status, 'pending')
        resumed = open_joint(self.analysis, self.channel, 'J-pend', revision.revision_id,
                             [('甲', ['A', 'B']), ('乙', ['C', 'D'])])
        self.assertEqual(resumed['status'], 'ready')
        self.assertEqual([item.envelope.dispatch_id for item in resumed['assignments']],
                         ['甲/1', '甲/2', '乙/1', '乙/2'])
        # Once confirmed, the ownership is fixed and the same request stays ready.
        stable = open_joint(self.analysis, DeliveryChannel(None), 'J-pend', revision.revision_id,
                            [('甲', ['A', 'B']), ('乙', ['C', 'D'])])
        self.assertEqual(stable['status'], 'ready')
        self.assertEqual([item.envelope.dispatch_id for item in stable['assignments']],
                         [item.envelope.dispatch_id for item in resumed['assignments']])


class JointDeliveryTests(JointFixture):
    def test_deliver_joint_sends_only_named_dispatches_in_order(self):
        _, result = self.open_full()
        envelopes = self.assignments_by_specimen(result)
        outcomes = deliver_joint(self.analysis, self.channel, 'J-full',
                                 [envelopes['C'].dispatch_id, envelopes['A'].dispatch_id])
        self.assertEqual([item.status for item in outcomes], ['waiting', 'waiting'])
        self.assertEqual([item.envelope for item in outcomes], [envelopes['C'], envelopes['A']])
        self.assertIsNone(read_dispatch_outcome(self.field, envelopes['D'].dispatch_id))
        self.assertIsNone(read_dispatch_outcome(self.field, envelopes['B'].dispatch_id))
        # Re-sending the original id is idempotent.
        again = deliver_joint(self.analysis, self.channel, 'J-full', [envelopes['C'].dispatch_id])
        self.assertEqual(again[0].envelope, envelopes['C'])

    def test_deliver_joint_rejects_unknown_and_foreign_inputs(self):
        _, result = self.open_full()
        other_batch = self.catalog.record('other-coating', None, [('A', 9), ('B', 8)])
        foreign = open_case(self.analysis, 'single', other_batch.revision_id)[0]
        with self.assertRaisesRegex(ValueError, 'does not belong to joint task'):
            deliver_joint(self.analysis, self.channel, 'J-full', [foreign.envelope.dispatch_id])
        with self.assertRaisesRegex(ValueError, 'does not belong to joint task'):
            deliver_joint(self.analysis, self.channel, 'J-full', ['甲/99'])
        with self.assertRaisesRegex(ValueError, 'unknown joint task'):
            deliver_joint(self.analysis, self.channel, 'J-missing', ['甲/1'])
        with self.assertRaisesRegex(ValueError, 'nonempty'):
            deliver_joint(self.analysis, self.channel, 'J-full', [])

    def test_offline_delivery_returns_none_without_losing_the_reservation(self):
        revision = self.record_joint()
        pending = open_joint(self.analysis, DeliveryChannel(None), 'J-off', revision.revision_id,
                             [('甲', ['A', 'B']), ('乙', ['C', 'D'])])
        self.assertEqual(pending['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'not ready for delivery'):
            deliver_joint(self.analysis, self.channel, 'J-off', ['甲/1'])

    def test_misroute_and_bad_permit_are_refused_without_occupying_dispatch(self):
        _, result = self.open_full()
        envelopes = self.assignments_by_specimen(result)
        target = envelopes['D']
        permit = joint_permit('J-full', target.dispatch_id)
        misroute = replace(target, specimen_id='A', micrometres=Fraction(10))
        with self.assertRaisesRegex(ValueError, 'fixed ownership'):
            self.channel.send_joint('J-full', misroute, permit=permit)
        with self.assertRaisesRegex(ValueError, 'permit'):
            self.channel.send_joint('J-full', target, permit='forged')
        with self.assertRaisesRegex(ValueError, 'different task'):
            self.channel.send_joint('J-other', target, permit=permit)
        self.assertIsNone(read_dispatch_outcome(self.field, target.dispatch_id))
        # The legitimate piece with the original permit and dispatch id still arrives.
        outcome = self.channel.send_joint('J-full', target, permit=permit)
        self.assertEqual((outcome.status, outcome.envelope), ('waiting', target))

    def test_legacy_send_path_cannot_bypass_joint_ownership(self):
        _, result = self.open_full()
        envelopes = self.assignments_by_specimen(result)
        target = envelopes['B']
        with self.assertRaisesRegex(ValueError, 'fixed ownership'):
            self.channel.send(replace(target, specimen_id='D', micrometres=Fraction(7)))
        self.assertIsNone(read_dispatch_outcome(self.field, target.dispatch_id))
        self.assertEqual(self.channel.send(target).envelope, target)

    def test_send_joint_before_manifest_is_pending_and_retryable(self):
        revision = self.record_joint()
        result = open_joint(self.analysis, DeliveryChannel(None), 'J-late', revision.revision_id,
                            [('甲', ['A', 'B']), ('乙', ['C', 'D'])])
        self.assertEqual(result['status'], 'pending')
        envelope = DispatchLedger(self.analysis).get('甲/1').envelope
        self.assertIsNone(self.channel.send_joint('J-late', envelope))
        open_joint(self.analysis, self.channel, 'J-late', revision.revision_id,
                   [('甲', ['A', 'B']), ('乙', ['C', 'D'])])
        outcome = self.channel.send_joint('J-late', envelope,
                                          permit=joint_permit('J-late', '甲/1'))
        self.assertEqual(outcome.status, 'waiting')


class JointFieldFlowTests(JointFixture):
    def test_batched_delivery_offline_claim_sync_and_separate_finalization(self):
        revision, result = self.open_full()
        envelopes = self.assignments_by_specimen(result)
        c, d, a, b = (envelopes[name] for name in ('C', 'D', 'A', 'B'))
        deliver_joint(self.analysis, self.channel, 'J-full', [c.dispatch_id])
        # Analysis workstation disconnects: the field still claims arrived pieces.
        self.analysis.close()
        first = receive(self.field, c)
        self.assertEqual((first.status, first.receipt.envelope), ('claimed', c))
        for envelope in (d, a, b):
            with self.assertRaisesRegex(ValueError, 'has not arrived'):
                receive(self.field, envelope)
        # Repeated receive replays the same original first receipt.
        self.assertEqual(receive(self.field, c), first)
        self.analysis = connect(self.analysis_path)
        self.channel = DeliveryChannel(self.field)
        deliver_joint(self.analysis, self.channel, 'J-full',
                      [d.dispatch_id, a.dispatch_id, b.dispatch_id])
        field_receipts = {envelope.specimen_id: receive(self.field, envelope).receipt
                          for envelope in (d, a, b)}
        # Lost replies are recovered through the original dispatch ids.
        recovered = sync_dispatch(self.analysis, self.channel, c.dispatch_id)
        self.assertEqual(recovered.receipt, first.receipt)
        for specimen, receipt in field_receipts.items():
            synced = sync_dispatch(self.analysis, self.channel,
                                   envelopes[specimen].dispatch_id)
            self.assertEqual(synced.receipt, receipt)
        report_jia = finalize(self.analysis, '甲', (first.receipt, field_receipts['D']))
        report_yi = finalize(self.analysis, '乙', (field_receipts['A'], field_receipts['B']))
        self.assertEqual(str(report_jia.mean_micrometres), '15/2')
        self.assertEqual(str(report_yi.mean_micrometres), '19/2')
        self.assertEqual([line.specimen_id for line in report_jia.lines], ['C', 'D'])
        self.assertTrue(all(line.revision_id == revision.revision_id for line in report_jia.lines))
        self.assertEqual(read(self.analysis, '甲'), report_jia)
        self.assertEqual(read(self.analysis, '乙'), report_yi)
        self.assertEqual(finalize(self.analysis, '甲', (first.receipt, field_receipts['D'])), report_jia)

    def test_reopen_preserves_ownership_and_first_receipts(self):
        _, result = self.open_full()
        envelopes = self.assignments_by_specimen(result)
        c = envelopes['C']
        retry_dispatch(self.analysis, self.channel, c.dispatch_id)
        self.reopen()
        self.assertEqual(JointTaskStore(self.analysis).require_task('J-full').status, 'ready')
        receipt = receive(self.field, c).receipt
        self.assertEqual(sync_dispatch(self.analysis, self.channel, c.dispatch_id).receipt, receipt)
        self.assertEqual(JointManifest(self.field).lookup_dispatch(c.dispatch_id)['envelope'], c)

    def test_in_transit_reservation_cannot_be_taken_by_another_case(self):
        revision = self.record_joint()
        result = open_joint(self.analysis, self.channel, 'J-hold', revision.revision_id,
                            [('甲', ['A', 'B']), ('乙', ['C', 'D'])])
        envelopes = self.assignments_by_specimen(result)
        # Nothing delivered yet: every dispatch remains unclaimable.
        with self.assertRaisesRegex(ValueError, 'has not arrived'):
            receive(self.field, envelopes['A'])
        deliver_joint(self.analysis, self.channel, 'J-hold', [envelopes['A'].dispatch_id])
        claimed = receive(self.field, envelopes['A'])
        self.assertEqual(claimed.receipt.envelope.case_id, '甲')
        # A foreign-looking piece reusing A's identity cannot bind to the second case.
        foreign = replace(envelopes['A'], case_id='乙', dispatch_id='乙/1')
        with self.assertRaisesRegex(ValueError, 'has not arrived'):
            receive(self.field, foreign)

    def test_correction_is_rejected_for_joint_cases_but_standalone_cases_keep_working(self):
        revision, _ = self.open_full()
        later = self.catalog.record('joint-coating', revision.revision_id,
                                    [('A', 1), ('B', 2), ('C', 3), ('D', 4)])
        with self.assertRaisesRegex(ValueError, 'joint review cases cannot be replanned'):
            correct(self.analysis, self.channel, '甲', 'req', later.revision_id)
        # V1 standalone behavior is unchanged.
        root = self.catalog.record('standalone', None, [('A', 9), ('B', 8), ('C', 7)])
        standalone = open_case(self.analysis, 'single', root.revision_id)
        receipts = tuple(retry_dispatch(self.analysis, self.channel, item.envelope.dispatch_id).receipt
                         for item in standalone)
        self.assertEqual(str(finalize(self.analysis, 'single', receipts).mean_micrometres), '17/2')
