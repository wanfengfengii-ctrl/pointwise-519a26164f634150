import argparse
from contextlib import ExitStack, closing
from dataclasses import dataclass, fields, is_dataclass, replace
from fractions import Fraction
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from review.close.history import ReportArchive, read
from review.close.publish import finalize
from review.field.acceptance import receive
from review.field.handover import list_case_receipts, read_dispatch_outcome
from review.field.inbox import FieldInbox
from review.field.joint import JointManifest, joint_permit
from review.field.receipt import Receipt, ReceiptLedger
from review.handoff.ack import AckLedger
from review.handoff.delivery import DeliveryChannel, DeliveryJournal
from review.handoff.retry import deliver_joint, resume_delivery, retry_dispatch, sync_dispatch
from review.measure.catalog import MeasurementCatalog
from review.pick.case import CaseStore, open as open_case
from review.pick.dispatch import DispatchLedger
from review.pick.joint import JointTaskStore, open_joint
from review.pick.progress import ProgressLedger
from review.pick.replan import CorrectionStore, correct
from review.storage import connect


SAMPLES = Path(__file__).resolve().parents[2] / 'samples'


def load_measurements(path):
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not {'batch_id', 'parent_id', 'readings'} <= payload.keys():
        raise ValueError('measurement input requires batch_id, parent_id and readings')
    return payload


def initialize(analysis, field, sample=None):
    catalog = MeasurementCatalog(analysis)
    catalog.install()
    for ledger in (CaseStore, DispatchLedger, ProgressLedger, AckLedger, DeliveryJournal,
                   CorrectionStore, ReportArchive, JointTaskStore):
        ledger(analysis).install()
    FieldInbox(field).install()
    ReceiptLedger(field).install()
    JointManifest(field).install()
    if sample is None:
        return None
    if sample['parent_id'] is not None:
        raise ValueError('initial sample must be a root revision')
    return catalog.ensure_sample(sample['batch_id'], sample['readings'])


def json_value(value):
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, Receipt):
        return {'receipt_id': value.receipt_id, 'sequence': value.sequence,
                'envelope': json_value(value.envelope)}
    if is_dataclass(value):
        return {item.name: json_value(getattr(value, item.name)) for item in fields(value)
                if item.init}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def _demo_route(directory, case_id, committed_first):
    root_input = load_measurements(SAMPLES / 'root.json')
    correction_input = load_measurements(SAMPLES / 'correction.json')
    analysis_path = directory / f'{case_id}-analysis.sqlite3'
    field_path = directory / f'{case_id}-field.sqlite3'
    with closing(connect(analysis_path, create=True)) as analysis, closing(connect(field_path, create=True)) as field:
        root = initialize(analysis, field, root_input)
        initial = open_case(analysis, case_id, root.revision_id)
        a, b = (assignment.envelope for assignment in initial)
        channel = DeliveryChannel(field)
        resume_delivery(analysis, channel, case_id)
        first = retry_dispatch(analysis, channel, a.dispatch_id).receipt
        revision = MeasurementCatalog(analysis).record(correction_input['batch_id'], root.revision_id,
                                                       correction_input['readings'])
        prior_b = receive(field, b).receipt if committed_first else None

    with closing(connect(analysis_path)) as analysis, closing(connect(field_path)) as field:
        channel = DeliveryChannel(field)
        correction = correct(analysis, channel, case_id, f'{case_id}-correct', revision.revision_id)
        replay = receive(field, b)
        if committed_first:
            second = retry_dispatch(analysis, channel, b.dispatch_id).receipt
        else:
            if len(correction.dispatches) != 1:
                raise ValueError('demo expected exactly one replacement')
            replacement = correction.dispatches[0].envelope
            second = retry_dispatch(analysis, channel, replacement.dispatch_id).receipt
        report = finalize(analysis, case_id, (first, second))
        if report is None:
            raise ValueError('demo could not verify two actual receipts')

    with closing(connect(analysis_path)) as analysis:
        saved = read(analysis, case_id)
    expected = {'correction': 'effective', 'old_b': 'claimed' if committed_first else 'refused',
                'replacements': [] if committed_first else ['C'], 'same_b_receipt': committed_first}
    actual = {'correction': correction.status, 'old_b': replay.status,
              'replacements': [item.envelope.specimen_id for item in correction.dispatches],
              'same_b_receipt': prior_b is not None and prior_b == replay.receipt}
    downstream_expected = {
        'lines': [['A', 1, '9'], ['B', 1, '8'] if committed_first else ['C', 2, '10']],
        'mean': '17/2' if committed_first else '19/2', 'archive_matches': True,
    }
    downstream_actual = {
        'lines': [[line.specimen_id, line.revision_id, str(line.micrometres)] for line in report.lines],
        'mean': str(report.mean_micrometres), 'archive_matches': saved == report,
    }
    if actual != expected or downstream_actual != downstream_expected:
        raise ValueError(f'{case_id} demo outcome does not match the review evidence')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': case_id,
        'input': {'root': root_input['readings'], 'correction': correction_input['readings'],
                  'b_claimed_before_correction': committed_first},
        'expected': expected, 'actual': actual,
        'downstream_expected': downstream_expected, 'downstream_actual': downstream_actual,
        'code_paths': ['review/pick/replan.py', 'review/field/acceptance.py',
                       'review/close/publish.py', 'review/close/history.py'],
        'oracle': 'Keep A at 9. With B already claimed, use (9+8)/2; otherwise C ranks first among unclaimed specimens, so use (9+10)/2.',
    }, sort_keys=True))


def _joint_demo():
    """Exercise the full JOINT_REVIEW.md sample script on fresh workstations."""
    fixture = json.loads((SAMPLES / 'joint.json').read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='coating-joint-demo-') as directory:
        base = Path(directory)
        analysis_path = base / 'joint-analysis.sqlite3'
        field_path = base / 'joint-field.sqlite3'
        with closing(connect(analysis_path, create=True)) as analysis, \
                closing(connect(field_path, create=True)) as field:
            initialize(analysis, field)
            channel = DeliveryChannel(field)
            revision = MeasurementCatalog(analysis).record(**fixture['measurements'])
            full = open_joint(analysis, channel, fixture['full']['task_id'], revision.revision_id,
                              fixture['full']['cases'])
            if full['status'] != 'ready':
                raise ValueError('joint demo expected a ready full task')
            ownership = {}
            for assignment in full['assignments']:
                ownership.setdefault(assignment.envelope.case_id, []).append(
                    assignment.envelope.specimen_id)
            envelopes = {item.envelope.specimen_id: item.envelope for item in full['assignments']}
            first_id = envelopes[fixture['full']['first'][0][1]].dispatch_id

            # Batch one: only 甲/C is sent, then the analysis workstation disconnects.
            first_outcome = deliver_joint(analysis, channel, fixture['full']['task_id'], [first_id])
            claimable_before_disconnect = {
                name: read_dispatch_outcome(field, envelope.dispatch_id) is not None
                for name, envelope in envelopes.items()}
        with closing(connect(field_path)) as field:
            # The field claims the arrived piece using only its own workstation.
            first_receipt = receive(field, envelopes['C']).receipt
            blocked = {}
            for name in ('D', 'A', 'B'):
                try:
                    receive(field, envelopes[name])
                    blocked[name] = 'claimed'
                except ValueError:
                    blocked[name] = 'not_claimable'
        with closing(connect(analysis_path)) as analysis, \
                closing(connect(field_path)) as field:
            channel = DeliveryChannel(field)
            task_id = fixture['full']['task_id']
            # Misroute: keep the real dispatch id and permit but swap specimen and value.
            misroute_spec = fixture['full']['misroute']
            target = envelopes[misroute_spec['sent_specimen_id']]
            substitute = envelopes[misroute_spec['substitute_specimen_id']]
            forged = replace(target, specimen_id=substitute.specimen_id,
                             micrometres=substitute.micrometres)
            permit = joint_permit(task_id, target.dispatch_id)
            try:
                channel.send_joint(task_id, forged, permit=permit)
                misroute_result = 'accepted'
            except ValueError as error:
                misroute_result = str(error)
            if read_dispatch_outcome(field, target.dispatch_id) is not None:
                raise ValueError('misroute occupied the genuine dispatch binding')

            # Every remaining legitimate piece then arrives and is first-claimed.
            remaining_ids = [envelopes[spec].dispatch_id for _, spec in fixture['full']['remaining']]
            delivered = deliver_joint(analysis, channel, task_id, remaining_ids)
            field_receipts = {'C': receive(field, envelopes['C']).receipt}
            for name in ('D', 'A', 'B'):
                field_receipts[name] = receive(field, envelopes[name]).receipt
            # A lost first-claim reply is recovered through the original dispatch id.
            recovered = {name: sync_dispatch(analysis, channel, envelopes[name].dispatch_id).receipt
                         for name in ('C', 'D', 'A', 'B')}
            if any(recovered[name] != field_receipts[name] for name in recovered):
                raise ValueError('reconnected sync did not return the original first receipts')
            reports = {}
            for case_id, pair in (('甲', ('C', 'D')), ('乙', ('A', 'B'))):
                report = finalize(analysis, case_id,
                                   (field_receipts[pair[0]], field_receipts[pair[1]]))
                if report is None:
                    raise ValueError(f'joint demo could not finalize {case_id}')
                if read(analysis, case_id) != report:
                    raise ValueError(f'joint demo archive mismatch for {case_id}')
                reports[case_id] = {
                    'mean_micrometres': str(report.mean_micrometres),
                    'lines': [[line.specimen_id, line.revision_id, str(line.micrometres),
                               line.dispatch_id, line.receipt_id] for line in report.lines],
                }

            # Shortage and preference fixtures run in their own empty workstations.
            shortage = _joint_aux_demo(base, 'shortage', fixture)
            preference = _joint_aux_demo(base, 'preference', fixture)

        expected_ownership = {'甲': ['C', 'D'], '乙': ['A', 'B']}
        expected_blocked = {'D': 'not_claimable', 'A': 'not_claimable', 'B': 'not_claimable'}
        expected_report_means = {'甲': '15/2', '乙': '19/2'}
        actual = {
            'ownership': ownership,
            'first_delivery': [item.status for item in first_outcome],
            'arrived_before_disconnect': claimable_before_disconnect,
            'offline_first_claim': first_receipt.receipt_id,
            'blocked_before_arrival': blocked,
            'misroute': misroute_result,
            'remaining_delivery': [item.status for item in delivered],
            'receipts': {name: receipt.receipt_id for name, receipt in field_receipts.items()},
            'reports': reports,
            'shortage': shortage,
            'preference': preference,
        }
        expected = {
            'ownership': expected_ownership,
            'first_delivery': ['waiting'],
            'arrived_before_disconnect': {'C': True, 'D': False, 'A': False, 'B': False},
            'blocked_before_arrival': expected_blocked,
            'misroute': 'joint dispatch content does not match the fixed ownership',
            'remaining_delivery': ['waiting', 'waiting', 'waiting'],
            'report_means': expected_report_means,
        }
        for key, value in expected.items():
            if key == 'report_means':
                actual_means = {case_id: report['mean_micrometres']
                                for case_id, report in actual['reports'].items()}
                if actual_means != value:
                    raise ValueError(f'joint demo mismatch on report means: {actual_means} != {value}')
            elif actual[key] != value:
                raise ValueError(f'joint demo mismatch on {key}: {actual[key]} != {value}')
        if shortage != {'status': 'shortage', 'cases': ['甲', '乙'], 'candidates': ['A', 'B', 'C']}:
            raise ValueError(f'joint demo shortage mismatch: {shortage}')
        if preference != {'甲': ['A', 'D'], '乙': ['B', 'C']}:
            raise ValueError(f'joint demo preference mismatch: {preference}')
        print('POINTWISE_SCENARIO ' + json.dumps({
            'scenario': 'joint_review',
            'input': fixture['measurements'],
            'expected': expected, 'actual': actual,
            'code_paths': ['review/pick/joint.py', 'review/field/joint.py',
                           'review/handoff/delivery.py', 'review/handoff/retry.py',
                           'review/close/publish.py', 'review/close/history.py'],
            'oracle': 'Joint ownership is fixed before delivery; only arrived pieces are claimable '
                      'and misrouted content never occupies a real dispatch id.',
        }, ensure_ascii=False, sort_keys=True))


def _joint_aux_demo(base, name, fixture):
    analysis_path = base / f'joint-{name}-analysis.sqlite3'
    field_path = base / f'joint-{name}-field.sqlite3'
    with closing(connect(analysis_path, create=True)) as analysis, \
            closing(connect(field_path, create=True)) as field:
        initialize(analysis, field)
        revision = MeasurementCatalog(analysis).record(**fixture['measurements'])
        result = open_joint(analysis, DeliveryChannel(field), fixture[name]['task_id'],
                            revision.revision_id, fixture[name]['cases'])
        if result['status'] == 'shortage':
            return {'status': 'shortage', 'cases': list(result['cases']),
                    'candidates': list(result['candidates'])}
        ownership = {}
        for assignment in result['assignments']:
            ownership.setdefault(assignment.envelope.case_id, []).append(
                assignment.envelope.specimen_id)
        return ownership


def _demo():
    with TemporaryDirectory(prefix='coating-review-demo-') as directory:
        _demo_route(Path(directory), 'v3', False)
        _demo_route(Path(directory), 'v4', True)
    _joint_demo()


def _parser():
    parser = argparse.ArgumentParser(description='Coating specimen review workbench')
    parser.add_argument('--analysis', type=Path)
    parser.add_argument('--field', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init').add_argument('--sample', type=Path, default=SAMPLES / 'root.json')
    commands.add_parser('record').add_argument('input', type=Path)
    commands.add_parser('revision').add_argument('revision_id', type=int)
    opening = commands.add_parser('open')
    opening.add_argument('case_id')
    opening.add_argument('revision_id', type=int)
    correction = commands.add_parser('correct')
    correction.add_argument('case_id')
    correction.add_argument('request_id')
    correction.add_argument('revision_id', type=int)
    for name in ('deliver', 'dispatches', 'progress', 'field-receipts', 'read'):
        commands.add_parser(name).add_argument('case_id')
    for name in ('receive', 'retry', 'sync', 'attempts'):
        commands.add_parser(name).add_argument('dispatch_id')
    closing_case = commands.add_parser('finalize')
    closing_case.add_argument('case_id')
    closing_case.add_argument('receipt_ids', nargs='*')
    commands.add_parser('demo')
    return parser


def _run(args, analysis, field, sample):
    command = args.command
    channel = DeliveryChannel(field)
    if command == 'init':
        return initialize(analysis, field, sample)
    if command == 'record':
        return MeasurementCatalog(analysis).record(**{
            key: sample[key] for key in ('batch_id', 'parent_id', 'readings')
        })
    if command == 'revision':
        return MeasurementCatalog(analysis).read_revision(args.revision_id)
    if command == 'open':
        return open_case(analysis, args.case_id, args.revision_id)
    if command == 'correct':
        return correct(analysis, channel, args.case_id, args.request_id, args.revision_id)
    if command == 'deliver':
        return resume_delivery(analysis, channel, args.case_id)
    if command == 'retry':
        return retry_dispatch(analysis, channel, args.dispatch_id)
    if command == 'sync':
        return sync_dispatch(analysis, channel, args.dispatch_id)
    if command == 'receive':
        entry = FieldInbox(field).lookup(args.dispatch_id)
        if entry is None:
            raise ValueError(f'unknown field dispatch: {args.dispatch_id}')
        return receive(field, entry.envelope)
    if command == 'field-receipts':
        return list_case_receipts(field, args.case_id)
    if command == 'dispatches':
        return DispatchLedger(analysis).list_case_dispatches(args.case_id)
    if command == 'progress':
        CaseStore(analysis).require(args.case_id)
        return ProgressLedger(analysis).snapshot(args.case_id)
    if command == 'attempts':
        return DeliveryJournal(analysis).for_dispatch(args.dispatch_id)
    if command == 'finalize':
        return finalize(analysis, args.case_id, args.receipt_ids)
    if command == 'read':
        return read(analysis, args.case_id)
    raise ValueError(f'unsupported workbench command: {command}')


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    command = args.command
    field_only = command in {'receive', 'field-receipts'}
    require_field = field_only or command in {'init', 'deliver', 'retry', 'sync'}
    if command != 'demo' and not field_only and args.analysis is None:
        parser.error(f'{command} requires --analysis')
    if require_field and args.field is None:
        parser.error(f'{command} requires --field')
    if command == 'init' and (args.analysis.resolve() == args.field.resolve()
                              or (args.analysis.exists() and args.field.exists()
                                  and args.analysis.samefile(args.field))):
        parser.error('analysis and field stores must use distinct files')
    try:
        if command == 'demo':
            _demo()
            return 0
        sample = None
        if command in {'init', 'record'}:
            sample = load_measurements(args.sample if command == 'init' else args.input)
        create = command == 'init'
        with ExitStack() as stack:
            analysis = None if field_only else stack.enter_context(closing(connect(args.analysis, create=create)))
            field = None
            if (require_field or command == 'correct') and args.field is not None:
                field = stack.enter_context(closing(connect(args.field, create=create)))
            result = _run(args, analysis, field, sample)
            print(json.dumps(json_value(result), ensure_ascii=False, sort_keys=True))
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f'workbench: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
