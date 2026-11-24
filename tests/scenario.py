from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from review.handoff.delivery import DeliveryChannel, FieldUnavailable
from review.handoff.workbench import initialize
from review.measure.catalog import MeasurementCatalog
from review.storage import connect


class ControlledChannel(DeliveryChannel):
    def __init__(self, field_db):
        super().__init__(field_db)
        self.pause_before_claim = set()
        self.drop_claim_reply = set()
        self.drop_retire_reply = set()

    def claim(self, envelope):
        if envelope.dispatch_id in self.pause_before_claim:
            raise FieldUnavailable('claim paused before the field transaction')
        result = super().claim(envelope)
        if envelope.dispatch_id in self.drop_claim_reply:
            self.drop_claim_reply.remove(envelope.dispatch_id)
            raise FieldUnavailable('claim reply lost after the field commit')
        return result

    def retire(self, envelope):
        result = super().retire(envelope)
        if envelope.dispatch_id in self.drop_retire_reply:
            self.drop_retire_reply.remove(envelope.dispatch_id)
            raise FieldUnavailable('retirement reply lost after the field commit')
        return result


class ProjectTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory(prefix='coating-review-test-'))
        self.analysis_path = Path(directory) / 'analysis.sqlite3'
        self.field_path = Path(directory) / 'field.sqlite3'
        self.analysis = connect(self.analysis_path, create=True)
        self.addCleanup(lambda: self.analysis.close())
        self.field = connect(self.field_path, create=True)
        self.addCleanup(lambda: self.field.close())
        initialize(self.analysis, self.field)
        self.channel = ControlledChannel(self.field)

    @property
    def catalog(self):
        return MeasurementCatalog(self.analysis)

    def reopen(self, *, analysis=True, field=True):
        if analysis:
            self.analysis.close()
            self.analysis = connect(self.analysis_path)
        if field:
            self.field.close()
            self.field = connect(self.field_path)
            self.channel = ControlledChannel(self.field)
