from fractions import Fraction

from review.close.history import read
from review.close.publish import finalize
from review.field.acceptance import receive
from review.handoff.retry import retry_dispatch
from review.handoff.workbench import SAMPLES, initialize, load_measurements
from review.pick.case import open as open_case
from review.pick.replan import correct
from tests.scenario import ProjectTest


class ArchiveTests(ProjectTest):
    def test_v6_reinitialization_preserves_report_and_user_revision(self):
        sample = load_measurements(SAMPLES / 'root.json')
        root = initialize(self.analysis, self.field, sample)
        assignments = open_case(self.analysis, 'archived', root.revision_id)
        receipts = tuple(retry_dispatch(self.analysis, self.channel, item.envelope.dispatch_id).receipt
                         for item in assignments)
        original = finalize(self.analysis, 'archived', receipts)
        self.assertEqual(original.mean_micrometres, Fraction(17, 2))
        self.assertEqual([line.specimen_id for line in original.lines], ['A', 'B'])
        user = self.catalog.record(root.batch_id, root.revision_id, [('A', 1), ('B', 2), ('C', 3)])
        changed = correct(self.analysis, self.channel, 'archived', 'late-notes', user.revision_id)
        self.assertEqual((changed.status, changed.dispatches), ('effective', ()))
        self.assertEqual(initialize(self.analysis, self.field, sample), root)
        self.reopen()
        self.assertEqual(initialize(self.analysis, self.field, sample), root)
        self.assertEqual(self.catalog.current_revision(root.batch_id), user)
        self.assertEqual(self.catalog.read_revision(user.revision_id), user)
        self.assertEqual(read(self.analysis, 'archived'), original)
        self.assertEqual(tuple(receive(self.field, item.envelope).receipt for item in assignments), receipts)

    def test_v7_exact_fractional_mean(self):
        root = self.catalog.record('fractions', None, [('little', '1/3'), ('large', '2/3')])
        assignments = open_case(self.analysis, 'fractions', root.revision_id)
        receipts = tuple(retry_dispatch(self.analysis, self.channel, item.envelope.dispatch_id).receipt
                         for item in assignments)
        report = finalize(self.analysis, 'fractions', receipts)
        self.assertEqual([item.micrometres for item in report.lines], [Fraction(2, 3), Fraction(1, 3)])
        self.assertEqual((report.mean_micrometres.numerator, report.mean_micrometres.denominator), (1, 2))
        self.reopen()
        saved = read(self.analysis, 'fractions')
        self.assertEqual((saved.mean_micrometres.numerator, saved.mean_micrometres.denominator), (1, 2))
        self.assertEqual(saved, report)

    def test_v8_incomplete_then_complete(self):
        root = self.catalog.record('lot', None, [('A', 9), ('B', 8)])
        a, b = open_case(self.analysis, 'case', root.revision_id)
        first = retry_dispatch(self.analysis, self.channel, a.envelope.dispatch_id).receipt
        self.assertIsNone(finalize(self.analysis, 'case', (first,)))
        self.assertIsNone(read(self.analysis, 'case'))
        second = retry_dispatch(self.analysis, self.channel, b.envelope.dispatch_id).receipt
        report = finalize(self.analysis, 'case', (first, second))
        self.assertEqual([line.receipt_id for line in report.lines], [first.receipt_id, second.receipt_id])
        self.assertEqual(report.mean_micrometres, Fraction(17, 2))
        self.assertEqual(finalize(self.analysis, 'case', (second, first)), report)
        self.assertIsNone(finalize(self.analysis, 'case', (first,)))
        self.assertEqual(read(self.analysis, 'case'), report)
        self.assertEqual(self.analysis.execute('SELECT COUNT(*) FROM reports').fetchone()[0], 1)
        self.assertEqual(self.analysis.execute('SELECT COUNT(*) FROM report_lines').fetchone()[0], 2)
