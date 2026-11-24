from fractions import Fraction

from review.close.history import read
from review.close.publish import finalize
from review.field.acceptance import receive
from review.field.handover import list_case_receipts, read_dispatch_outcome
from review.handoff.ack import AckLedger
from review.handoff.delivery import DeliveryChannel, DeliveryJournal
from review.handoff.retry import resume_delivery, retry_dispatch
from review.pick.case import CaseStore, open as open_case
from review.pick.progress import ProgressLedger
from review.pick.replan import correct
from tests.scenario import ProjectTest


CORRECTION_MARK = 'REVIEW_PICK_CORRECTION'


class RevisionTests(ProjectTest):
    def start_case(self):
        root = self.catalog.record('lot', None, [('A', 9), ('B', 8), ('C', 7)])
        a, b = (item.envelope for item in open_case(self.analysis, 'case', root.revision_id))
        resume_delivery(self.analysis, self.channel, 'case')
        first = retry_dispatch(self.analysis, self.channel, a.dispatch_id).receipt
        return root, a, b, first

    def corrected_values(self, root):
        return self.catalog.record('lot', root.revision_id, [('A', 0), ('B', 7), ('C', 10)])

    def test_v3_correction_precedes_first_claim(self):
        root, a, b, first = self.start_case()
        self.channel.pause_before_claim.add(b.dispatch_id)
        self.assertIsNone(retry_dispatch(self.analysis, self.channel, b.dispatch_id), CORRECTION_MARK)
        self.assertEqual(read_dispatch_outcome(self.field, b.dispatch_id).status, 'waiting', CORRECTION_MARK)
        revised = self.corrected_values(root)
        result = correct(self.analysis, self.channel, 'case', 'k', revised.revision_id)
        self.assertEqual(result.status, 'effective', CORRECTION_MARK)
        self.reopen()
        late_b = receive(self.field, b)
        self.assertEqual((late_b.status, late_b.reason, late_b.receipt),
                         ('refused', 'superseded_dispatch', None), CORRECTION_MARK)
        self.assertEqual(receive(self.field, a).receipt, first, CORRECTION_MARK)
        self.assertEqual(list_case_receipts(self.field, 'case'), (first,), CORRECTION_MARK)
        self.assertIsNone(finalize(self.analysis, 'case', (first,)), CORRECTION_MARK)
        self.assertIsNone(read(self.analysis, 'case'), CORRECTION_MARK)
        self.assertEqual(len(result.dispatches), 1, CORRECTION_MARK)
        replacement = result.dispatches[0].envelope
        self.assertEqual((replacement.specimen_id, replacement.revision_id, replacement.micrometres),
                         ('C', revised.revision_id, Fraction(10)), CORRECTION_MARK)
        self.assertNotEqual(replacement.dispatch_id, b.dispatch_id, CORRECTION_MARK)
        second = retry_dispatch(self.analysis, self.channel, replacement.dispatch_id).receipt
        report = finalize(self.analysis, 'case', (second, first))
        self.assertIsNotNone(report, CORRECTION_MARK)
        self.assertEqual([(line.specimen_id, line.revision_id, line.micrometres, line.receipt_id)
                          for line in report.lines],
                         [('A', root.revision_id, Fraction(9), first.receipt_id),
                          ('C', revised.revision_id, Fraction(10), second.receipt_id)], CORRECTION_MARK)
        self.assertEqual(report.mean_micrometres, Fraction(19, 2), CORRECTION_MARK)
        self.reopen()
        self.assertEqual(read(self.analysis, 'case'), report, CORRECTION_MARK)
        self.assertEqual(receive(self.field, b), late_b, CORRECTION_MARK)
        self.assertEqual(receive(self.field, replacement).receipt, second, CORRECTION_MARK)
        self.assertEqual(correct(self.analysis, self.channel, 'case', 'k', revised.revision_id),
                         result, CORRECTION_MARK)

    def test_v4_first_claim_precedes_correction_and_reply_is_lost(self):
        root, a, b, first = self.start_case()
        self.channel.drop_claim_reply.add(b.dispatch_id)
        self.assertIsNone(retry_dispatch(self.analysis, self.channel, b.dispatch_id), CORRECTION_MARK)
        self.assertIsNone(AckLedger(self.analysis).get_verified_receipt(b.dispatch_id), CORRECTION_MARK)
        committed = list_case_receipts(self.field, 'case')
        self.assertEqual(len(committed), 2, CORRECTION_MARK)
        second = committed[1]
        self.assertEqual(second.envelope, b, CORRECTION_MARK)
        self.reopen()
        revised = self.corrected_values(root)
        result = correct(self.analysis, self.channel, 'case', 'k', revised.revision_id)
        self.assertEqual((result.status, result.dispatches), ('effective', ()), CORRECTION_MARK)
        recovered = retry_dispatch(self.analysis, self.channel, b.dispatch_id)
        self.assertIsNotNone(recovered, CORRECTION_MARK)
        self.assertEqual(recovered.receipt, second, CORRECTION_MARK)
        self.assertEqual(receive(self.field, a).receipt, first, CORRECTION_MARK)
        self.assertEqual(list_case_receipts(self.field, 'case'), committed, CORRECTION_MARK)
        last = DeliveryJournal(self.analysis).for_dispatch(b.dispatch_id)[-1]
        self.assertEqual((last['dispatch_id'], last['operation'], last['status']),
                         (b.dispatch_id, 'send', 'claimed'), CORRECTION_MARK)
        report = finalize(self.analysis, 'case', (second.receipt_id, first.receipt_id))
        self.assertIsNotNone(report, CORRECTION_MARK)
        self.assertEqual([(line.specimen_id, line.revision_id, line.micrometres, line.receipt_id)
                          for line in report.lines],
                         [('A', root.revision_id, Fraction(9), first.receipt_id),
                          ('B', root.revision_id, Fraction(8), second.receipt_id)], CORRECTION_MARK)
        self.assertEqual(report.mean_micrometres, Fraction(17, 2), CORRECTION_MARK)
        self.reopen()
        self.assertEqual(read(self.analysis, 'case'), report, CORRECTION_MARK)
        self.assertEqual(receive(self.field, b).receipt, second, CORRECTION_MARK)

    def test_v5_offline_correction_resumes_from_field_result(self):
        root, _, b, first = self.start_case()
        revised = self.corrected_values(root)
        pending = correct(self.analysis, DeliveryChannel(None), 'case', 'k', revised.revision_id)
        self.assertEqual((pending.status, pending.dispatches), ('pending', ()))
        self.assertEqual(CaseStore(self.analysis).require('case').effective_revision, root.revision_id)
        self.assertEqual(ProgressLedger(self.analysis).snapshot('case').receipts, (first,))
        second = receive(self.field, b).receipt
        self.reopen()
        effective = correct(self.analysis, self.channel, 'case', 'k', revised.revision_id)
        self.assertEqual((effective.status, effective.dispatches), ('effective', ()))
        self.assertEqual(ProgressLedger(self.analysis).snapshot('case').receipts, (first, second))
        self.assertEqual(CaseStore(self.analysis).require('case').effective_revision, revised.revision_id)
        self.assertEqual(finalize(self.analysis, 'case', (first, second)).mean_micrometres, Fraction(17, 2))
