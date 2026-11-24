def check_correction_lineage(catalog, current_id, target_id):
    current = catalog.read_revision(current_id)
    target = catalog.read_revision(target_id)
    if current.batch_id != target.batch_id:
        raise ValueError('correction revision belongs to another batch')
    ancestor = target.parent_id
    while ancestor is not None:
        if ancestor == current_id:
            return target
        ancestor = catalog.parent_of(ancestor)
    raise ValueError('correction must use a later revision of the case batch')
