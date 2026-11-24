from fractions import Fraction

from review.close.publish import finalize
from review.handoff.retry import resume_delivery
from review.pick.case import CaseStore, open as open_case
from review.pick.dispatch import DispatchLedger
from review.pick.replan import correct
from tests.scenario import ProjectTest


class MeasurementEdgesTests(ProjectTest):
    def test_v1_duplicate_reading_does_not_advance_batch(self):
        with self.assertRaisesRegex(ValueError, 'duplicate specimen'):
            self.catalog.record('lot', None, [('B', 8), ('A', 9), ('B', 11)])
        self.assertIsNone(self.catalog.current_revision('lot'))
        with self.assertRaisesRegex(ValueError, 'unknown revision'):
            self.catalog.read_revision(1)
        root = self.catalog.record('lot', None, [('B', 9), ('A', 9)])
        with self.assertRaisesRegex(ValueError, 'duplicate specimen'):
            self.catalog.record('lot', root.revision_id, [('A', 7), ('A', 11)])
        self.assertEqual(self.catalog.current_revision('lot'), root)
        open_case(self.analysis, 'case', root.revision_id)
        receipts = [outcome.receipt for outcome in resume_delivery(self.analysis, self.channel, 'case', claim=True)]
        self.assertEqual([receipt.envelope.specimen_id for receipt in receipts], ['A', 'B'])
        self.assertEqual(finalize(self.analysis, 'case', receipts).mean_micrometres, Fraction(9))

    def test_v1_bad_rational_rolls_back_revision(self):
        root = self.catalog.record('lot', None, [('A', '-1/3'), ('B', 2)])
        for value in (None, '1/0', 'not-a-number', 0.25, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.catalog.record('lot', root.revision_id, [('A', 3), ('B', value)])
            self.assertEqual(self.catalog.current_revision('lot'), root)
        with self.assertRaises(ValueError):
            self.catalog.record('lot', root.revision_id, [('A',)])
        with self.assertRaisesRegex(ValueError, 'unknown revision'):
            self.catalog.read_revision(root.revision_id + 1)
        revised = self.catalog.record('lot', root.revision_id, [('B', '6/9'), ('A', '-2/6')])
        self.assertEqual(revised.by_specimen['B'].micrometres, Fraction(2, 3))
        self.assertEqual(revised.by_specimen['A'].micrometres, Fraction(-1, 3))
        self.assertEqual(self.catalog.read_revision(root.revision_id), root)

    def test_v1_insufficient_candidates_never_create_partial_case(self):
        root = self.catalog.record('lot', None, [('A', '1/3')])
        with self.assertRaisesRegex(ValueError, 'two eligible specimens'):
            open_case(self.analysis, 'case', root.revision_id)
        self.assertIsNone(CaseStore(self.analysis).get('case'))
        self.assertEqual(DispatchLedger(self.analysis).list_case_dispatches('case'), ())
        revised = self.catalog.record('lot', root.revision_id, [('A', '1/3'), ('B', '2/3')])
        self.assertEqual([item.envelope.specimen_id for item in open_case(self.analysis, 'case', revised.revision_id)],
                         ['B', 'A'])

    def test_parent_and_correction_lineage(self):
        root = self.catalog.record('lot', None, [('A', 1), ('B', 2)])
        other = self.catalog.record('other', None, [('A', 3), ('B', 4)])
        with self.assertRaisesRegex(ValueError, 'current batch revision'):
            self.catalog.record('lot', other.revision_id, [('A', 5)])
        with self.assertRaisesRegex(ValueError, 'current batch revision'):
            self.catalog.record('new-lot', root.revision_id, [('A', 5)])
        self.assertIsNone(self.catalog.current_revision('new-lot'))
        self.assertEqual(self.catalog.current_revision('lot'), root)
        open_case(self.analysis, 'case', root.revision_id)
        with self.assertRaisesRegex(ValueError, 'another batch'):
            correct(self.analysis, self.channel, 'case', 'k', other.revision_id)
        middle = self.catalog.record('lot', root.revision_id, [('A', 6), ('B', 2)])
        latest = self.catalog.record('lot', middle.revision_id, [('A', 7), ('B', 2)])
        result = correct(self.analysis, self.channel, 'case', 'k', latest.revision_id)
        self.assertEqual(result.status, 'effective')
        with self.assertRaisesRegex(ValueError, 'later revision'):
            correct(self.analysis, self.channel, 'case', 'rollback', middle.revision_id)
