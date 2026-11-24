from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from review.measure.reading import Reading, require_name, require_revision_number


@dataclass(frozen=True)
class Revision:
    batch_id: str
    parent_id: int | None
    readings: tuple[Reading, ...]
    revision_id: int | None = None
    by_specimen: Mapping[str, Reading] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        require_name(self.batch_id, 'batch')
        if self.parent_id is not None:
            require_revision_number(self.parent_id)
        if self.revision_id is not None:
            require_revision_number(self.revision_id)
        readings = tuple(self.readings)
        seen = set()
        for reading in readings:
            if not isinstance(reading, Reading):
                raise ValueError('revision requires named readings')
            if reading.specimen_id in seen:
                raise ValueError(f'duplicate specimen: {reading.specimen_id}')
            seen.add(reading.specimen_id)
        object.__setattr__(self, 'readings', readings)
        object.__setattr__(self, 'by_specimen', MappingProxyType({
            reading.specimen_id: reading for reading in readings
        }))


def build_revision(batch_id, parent_id, rows):
    try:
        inputs = iter(rows)
    except TypeError as error:
        raise ValueError('readings must be a sequence of name and value pairs') from error
    readings = []
    for row in inputs:
        if isinstance(row, Reading):
            readings.append(row)
        else:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise ValueError('each reading must contain a specimen and micrometres')
            readings.append(Reading(row[0], row[1]))
    return Revision(batch_id, parent_id, tuple(readings))
