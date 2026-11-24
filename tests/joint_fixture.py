"""Joint review input and a comparison using existing standalone cases."""
from contextlib import closing
from fractions import Fraction
import json
from pathlib import Path
import sqlite3

from review.close.history import read
from review.close.publish import finalize
from review.handoff.delivery import DeliveryChannel
from review.handoff.retry import resume_delivery
from review.handoff.workbench import initialize
from review.measure.catalog import MeasurementCatalog
from review.pick.case import open as open_case


SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "joint.json"


def load_fixture():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def memory_db():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def main():
    fixture = load_fixture()
    reports = {}
    with closing(memory_db()) as analysis, closing(memory_db()) as field:
        initialize(analysis, field)
        revision = MeasurementCatalog(analysis).record(**fixture["measurements"])
        channel = DeliveryChannel(field)
        for case_id, _ in fixture["full"]["cases"]:
            open_case(analysis, case_id, revision.revision_id)
            outcomes = resume_delivery(analysis, channel, case_id, claim=True)
            if len(outcomes) != 2 or any(item is None or item.status != "claimed" for item in outcomes):
                raise ValueError(f"standalone case {case_id} did not receive both claims")
            report = finalize(analysis, case_id, tuple(item.receipt for item in outcomes))
            if report is None or report != read(analysis, case_id):
                raise ValueError(f"standalone case {case_id} could not be archived")
            specimens = [line.specimen_id for line in report.lines]
            if specimens != ["A", "B"] or report.mean_micrometres != Fraction(19, 2):
                raise ValueError(f"unexpected standalone assignment for {case_id}")
            reports[case_id] = {"specimens": specimens, "mean_micrometres": str(report.mean_micrometres)}
    print(json.dumps({"standalone_reports": reports}, ensure_ascii=False))


if __name__ == "__main__":
    main()
