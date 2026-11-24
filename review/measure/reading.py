from dataclasses import dataclass
from fractions import Fraction
import re


_RATIONAL = re.compile(r'([+-]?[0-9]+)(?:/([+-]?[0-9]+))?')


def require_name(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{label} must be a nonempty name')
    return value


def require_revision_number(value):
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError('revision number must be a positive SQLite integer')
    return value


def parse_micrometres(value):
    if isinstance(value, Fraction):
        return value
    if type(value) is int:
        return Fraction(value)
    if isinstance(value, str):
        match = _RATIONAL.fullmatch(value.strip())
        if match:
            numerator, denominator = match.groups()
            try:
                return Fraction(int(numerator), int(denominator or '1'))
            except ZeroDivisionError as error:
                raise ValueError('micrometre denominator cannot be zero') from error
    raise ValueError('micrometres must be an integer or an exact fraction')


@dataclass(frozen=True)
class Reading:
    specimen_id: str
    micrometres: Fraction

    def __post_init__(self):
        require_name(self.specimen_id, 'specimen')
        object.__setattr__(self, 'micrometres', parse_micrometres(self.micrometres))
