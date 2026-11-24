from review.handoff.ack import apply_field_outcome
from review.handoff.delivery import exchange
from review.pick.case import CaseStore
from review.pick.dispatch import DispatchLedger
from review.pick.joint import JointTaskStore


def _transfer(db, envelope, operation, perform):
    outcome = exchange(db, envelope, operation, perform)
    apply_field_outcome(db, envelope, outcome)
    return outcome


def retry_dispatch(db, channel, dispatch_id, *, claim=True):
    envelope = DispatchLedger(db).get(dispatch_id).envelope
    outcome = _transfer(db, envelope, 'send', channel.send)
    if claim and outcome is not None and outcome.status == 'waiting':
        return _transfer(db, envelope, 'claim', channel.claim)
    return outcome


def sync_dispatch(db, channel, dispatch_id):
    envelope = DispatchLedger(db).get(dispatch_id).envelope
    return _transfer(db, envelope, 'fetch', channel.fetch_outcome)


def resume_delivery(db, channel, case_id, *, claim=False):
    case = CaseStore(db).require(case_id)
    assignments = DispatchLedger(db).list_case_dispatches(case_id, revision_id=case.effective_revision)
    return tuple(retry_dispatch(db, channel, assignment.envelope.dispatch_id, claim=claim)
                 for assignment in assignments)


def deliver_joint(db, channel, task_id, dispatch_ids):
    """Send only the named dispatches of a fixed joint task, in request order."""
    task = JointTaskStore(db).require_task(task_id)
    if task.status != 'ready':
        raise ValueError(f'joint task is not ready for delivery: {task_id}')
    if not isinstance(dispatch_ids, (tuple, list)) or not dispatch_ids:
        raise ValueError('joint delivery requires a nonempty dispatch sequence')
    known = {assignment.envelope.dispatch_id: assignment
             for assignment in JointTaskStore(db).list_task_dispatches(task_id)}
    results = []
    for dispatch_id in dispatch_ids:
        if dispatch_id not in known:
            raise ValueError(f'dispatch does not belong to joint task {task_id}: {dispatch_id}')
        envelope = known[dispatch_id].envelope
        results.append(exchange(db, envelope, 'send', channel.send))
    return tuple(results)
