from dataclasses import dataclass
from fractions import Fraction

from review.measure.reading import parse_micrometres, require_name, require_revision_number


@dataclass(frozen=True)
class DispatchEnvelope:
    dispatch_id: str
    case_id: str
    specimen_id: str
    revision_id: int
    micrometres: Fraction

    def __post_init__(self):
        require_name(self.dispatch_id, 'dispatch')
        require_name(self.case_id, 'case')
        require_name(self.specimen_id, 'specimen')
        require_revision_number(self.revision_id)
        object.__setattr__(self, 'micrometres', parse_micrometres(self.micrometres))


def same_dispatch(left, right):
    return isinstance(left, DispatchEnvelope) and isinstance(right, DispatchEnvelope) and left == right


def envelope_values(envelope):
    return (envelope.dispatch_id, envelope.case_id, envelope.specimen_id,
            envelope.revision_id, str(envelope.micrometres))


def envelope_from_row(row):
    return DispatchEnvelope(row['dispatch_id'], row['case_id'], row['specimen_id'],
                            row['revision_id'], row['micrometres'])
