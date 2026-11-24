from dataclasses import replace
from fractions import Fraction

from review.close.history import read
from review.close.publish import finalize
from review.field.acceptance import receive
from review.field.handover import read_dispatch_outcome
from review.handoff.delivery import DeliveryChannel
from review.handoff.retry import resume_delivery, retry_dispatch, sync_dispatch
from review.pick.case import CaseStore, open as open_case
from review.pick.progress import ProgressLedger
from review.pick.replan import correct
from review.storage import connect
from tests.scenario import ProjectTest


class ExchangeEdgesTests(ProjectTest):
    def start_pair(self, case_id='case'):
        root = self.catalog.record('lot', None, [('A', 9), ('B', 8), ('C', 7)])
        assignments = open_case(self.analysis, case_id, root.revision_id)
        a, b = (item.envelope for item in assignments)
        return root, a, b

    def test_v1_offline_claim_preserves_field_order(self):
        _, a, b = self.start_pair()
        resume_delivery(self.analysis, self.channel, 'case')
        self.analysis.close()
        first = receive(self.field, b).receipt
        second = receive(self.field, a).receipt
        self.analysis = connect(self.analysis_path)
        self.assertEqual(sync_dispatch(self.analysis, self.channel, a.dispatch_id).receipt, second)
        self.assertEqual(sync_dispatch(self.analysis, self.channel, b.dispatch_id).receipt, first)
        report = finalize(self.analysis, 'case', (second, first))
        self.assertEqual([item.specimen_id for item in report.lines], ['B', 'A'])
        self.assertEqual([item.receipt_id for item in report.lines], [first.receipt_id, second.receipt_id])
        self.assertEqual(report.mean_micrometres, Fraction(17, 2))

    def test_v3_undelivered_old_dispatch_has_a_persistent_refusal(self):
        root, a, b = self.start_pair()
        first = retry_dispatch(self.analysis, self.channel, a.dispatch_id).receipt
        self.assertIsNone(read_dispatch_outcome(self.field, b.dispatch_id))
        revision = self.catalog.record('lot', root.revision_id, [('A', 99), ('B', 1), ('C', 10)])
        result = correct(self.analysis, self.channel, 'case', 'k', revision.revision_id)
        self.assertEqual(result.status, 'effective')
        self.reopen()
        refusal = self.channel.send(b)
        self.assertEqual((refusal.status, refusal.reason), ('refused', 'superseded_dispatch'))
        self.assertEqual(receive(self.field, b), refusal)
        with self.assertRaisesRegex(ValueError, 'bound to different inputs'):
            receive(self.field, replace(b, revision_id=revision.revision_id))
        replacement = result.dispatches[0].envelope
        self.assertEqual(replacement.specimen_id, 'C')
        second = retry_dispatch(self.analysis, self.channel, replacement.dispatch_id).receipt
        self.assertEqual(finalize(self.analysis, 'case', (first, second)).mean_micrometres, Fraction(19, 2))

    def test_v5_same_request_after_reopen_and_lost_retirement_reply(self):
        root, a, b = self.start_pair()
        first = retry_dispatch(self.analysis, self.channel, a.dispatch_id).receipt
        self.channel.send(b)
        revision = self.catalog.record('lot', root.revision_id, [('A', 0), ('B', 7), ('C', 10)])
        later = self.catalog.record('lot', revision.revision_id, [('A', 0), ('B', 12), ('C', 10)])
        waiting = correct(self.analysis, DeliveryChannel(None), 'case', 'k', revision.revision_id)
        self.assertEqual(waiting.status, 'pending')
        self.reopen()
        with self.assertRaisesRegex(ValueError, 'bound to different inputs'):
            correct(self.analysis, self.channel, 'case', 'k', later.revision_id)
        with self.assertRaisesRegex(ValueError, 'already has a pending correction'):
            correct(self.analysis, self.channel, 'case', 'other', later.revision_id)
        self.channel.drop_retire_reply.add(b.dispatch_id)
        self.assertEqual(correct(self.analysis, self.channel, 'case', 'k', revision.revision_id), waiting)
        self.assertEqual(receive(self.field, b).reason, 'superseded_dispatch')
        self.assertEqual(CaseStore(self.analysis).require('case').effective_revision, root.revision_id)
        self.reopen()
        effective = correct(self.analysis, self.channel, 'case', 'k', revision.revision_id)
        self.assertEqual(effective.status, 'effective')
        self.assertEqual([item.envelope.specimen_id for item in effective.dispatches], ['C'])
        replacement = correct(self.analysis, self.channel, 'case', 'later', later.revision_id)
        self.assertEqual([item.envelope.specimen_id for item in replacement.dispatches], ['B'])
        self.assertEqual(correct(self.analysis, self.channel, 'case', 'k', revision.revision_id), effective)
        self.assertEqual(ProgressLedger(self.analysis).snapshot('case').receipts, (first,))
        new_b = replacement.dispatches[0].envelope
        self.assertNotEqual(new_b.dispatch_id, b.dispatch_id)
        second = retry_dispatch(self.analysis, self.channel, new_b.dispatch_id).receipt
        self.assertEqual(finalize(self.analysis, 'case', (first, second)).mean_micrometres, Fraction(21, 2))

    def test_empty_correction_candidates_leave_vacancy_until_later_revision(self):
        root, a, b = self.start_pair()
        first = retry_dispatch(self.analysis, self.channel, a.dispatch_id).receipt
        depleted = self.catalog.record('lot', root.revision_id, [('A', 99)])
        empty = correct(self.analysis, self.channel, 'case', 'empty', depleted.revision_id)
        self.assertEqual((empty.status, empty.dispatches), ('effective', ()))
        self.assertEqual(ProgressLedger(self.analysis).snapshot('case').vacant_slots, (2,))
        self.assertEqual(receive(self.field, b).status, 'refused')
        self.assertIsNone(finalize(self.analysis, 'case', (first,)))
        replenished = self.catalog.record('lot', depleted.revision_id, [('A', 41), ('D', '3/2')])
        update = correct(self.analysis, self.channel, 'case', 'next', replenished.revision_id)
        self.assertEqual([(item.slot, item.envelope.specimen_id) for item in update.dispatches], [(2, 'D')])
        second = retry_dispatch(self.analysis, self.channel, update.dispatches[0].envelope.dispatch_id).receipt
        self.assertEqual(finalize(self.analysis, 'case', (first, second)).mean_micrometres, Fraction(21, 4))

    def test_v8_duplicate_foreign_and_changed_receipts_do_not_publish(self):
        root, a, b = self.start_pair()
        foreign, _ = open_case(self.analysis, 'another', root.revision_id)
        first = retry_dispatch(self.analysis, self.channel, a.dispatch_id).receipt
        other = retry_dispatch(self.analysis, self.channel, foreign.envelope.dispatch_id).receipt
        changed = replace(first, envelope=replace(first.envelope, micrometres=5))
        for evidence in ((first, first.receipt_id), (first, other), (first, changed), (first, 'receipt-999')):
            with self.subTest(evidence=evidence):
                self.assertIsNone(finalize(self.analysis, 'case', evidence))
                self.assertIsNone(read(self.analysis, 'case'))
        second = retry_dispatch(self.analysis, self.channel, b.dispatch_id).receipt
        self.assertIsNone(finalize(self.analysis, 'case', (changed, second)))
        report = finalize(self.analysis, 'case', (first, second))
        self.assertEqual([line.specimen_id for line in report.lines], ['A', 'B'])
        self.assertEqual(read(self.analysis, 'case'), report)
