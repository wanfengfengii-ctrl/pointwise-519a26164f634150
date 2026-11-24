from dataclasses import replace
from fractions import Fraction

from review.close.publish import finalize
from review.field.acceptance import receive
from review.field.handover import list_case_receipts
from review.handoff.delivery import DeliveryJournal
from review.handoff.retry import resume_delivery, retry_dispatch
from review.pick.case import open as open_case
from review.pick.progress import ProgressLedger
from tests.scenario import ProjectTest


class IssueTests(ProjectTest):
    def test_v1_ordered_issue(self):
        root = self.catalog.record('lot', None, [('B', '5/2'), ('A', '5/2'), ('C', 2)])
        self.assertEqual(tuple(root.by_specimen), ('B', 'A', 'C'))
        assignments = open_case(self.analysis, 'case', root.revision_id)
        a, b = (item.envelope for item in assignments)
        self.assertEqual([item.specimen_id for item in (a, b)], ['A', 'B'])
        delivered = resume_delivery(self.analysis, self.channel, 'case')
        self.assertEqual([item.status for item in delivered], ['waiting', 'waiting'])
        self.assertEqual([item.envelope for item in delivered], [a, b])
        self.assertEqual(ProgressLedger(self.analysis).snapshot('case').vacant_slots, (1, 2))
        journal = DeliveryJournal(self.analysis)
        self.assertEqual([row['status'] for row in journal.for_dispatch(a.dispatch_id)], ['delivered'])
        self.assertLess(journal.for_dispatch(a.dispatch_id)[0]['attempt_id'],
                        journal.for_dispatch(b.dispatch_id)[0]['attempt_id'])
        receipts = tuple(retry_dispatch(self.analysis, self.channel, item.dispatch_id).receipt for item in (a, b))
        self.assertEqual(list_case_receipts(self.field, 'case'), receipts)
        self.assertEqual([receipt.envelope.specimen_id for receipt in receipts], ['A', 'B'])
        self.assertLess(receipts[0].sequence, receipts[1].sequence)
        report = finalize(self.analysis, 'case', receipts)
        self.assertEqual(report.mean_micrometres, Fraction(5, 2))

    def test_v2_same_dispatch_changed_value_is_rejected(self):
        root = self.catalog.record('lot', None, [('A', 9), ('B', 8)])
        a, _ = open_case(self.analysis, 'case', root.revision_id)
        original = retry_dispatch(self.analysis, self.channel, a.envelope.dispatch_id)
        self.assertEqual(original.status, 'claimed')
        changed = replace(a.envelope, micrometres=Fraction(17, 2))
        with self.assertRaisesRegex(ValueError, 'bound to different inputs'):
            receive(self.field, changed)
        with self.assertRaisesRegex(ValueError, 'bound to different inputs'):
            self.channel.send(changed)
        self.reopen()
        self.assertEqual(receive(self.field, a.envelope), original)
        self.assertEqual(list_case_receipts(self.field, 'case'), (original.receipt,))
