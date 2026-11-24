def rank_candidates(revision):
    return tuple(sorted(revision.readings, key=lambda item: (-item.micrometres, item.specimen_id)))


def select_candidates(revision, count, exclude=()):
    if count < 0:
        raise ValueError('candidate count cannot be negative')
    excluded = frozenset(exclude)
    eligible = (item for item in rank_candidates(revision) if item.specimen_id not in excluded)
    from itertools import islice

    return tuple(islice(eligible, count))
