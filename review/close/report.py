from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class ReportLine:
    specimen_id: str
    revision_id: int
    micrometres: Fraction
    dispatch_id: str
    receipt_id: str
    sequence: int


@dataclass(frozen=True)
class Report:
    case_id: str
    lines: tuple[ReportLine, ...]
    mean_micrometres: Fraction


def build_report(case_id, receipts, mean_micrometres):
    lines = tuple(ReportLine(
        receipt.envelope.specimen_id, receipt.envelope.revision_id, receipt.envelope.micrometres,
        receipt.envelope.dispatch_id, receipt.receipt_id, receipt.sequence
    ) for receipt in receipts)
    return Report(case_id, lines, mean_micrometres)
