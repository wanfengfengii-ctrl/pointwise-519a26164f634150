from fractions import Fraction


def calculate_mean(receipts):
    values = tuple(receipt.envelope.micrometres for receipt in receipts)
    if len(values) != 2:
        raise ValueError('a review mean requires exactly two verified readings')
    return sum(values, start=Fraction(0)) / 2
