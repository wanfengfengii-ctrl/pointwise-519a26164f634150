from review.close.history import ReportArchive
from review.close.reconcile import reconcile_receipts
from review.close.report import build_report
from review.close.summary import calculate_mean
from review.storage import atomic


def finalize(db, case_id, evidence):
    archive = ReportArchive(db)
    with atomic(db):
        receipts = reconcile_receipts(db, case_id, evidence)
        if receipts is None:
            return None
        previous = archive.get(case_id)
        if previous is not None:
            return previous
        report = build_report(case_id, receipts, calculate_mean(receipts))
        return archive.store(report)
