"""Unsupported source dates cannot enter durable text through summarize."""
import tempfile
import unittest
from pathlib import Path
from memleaf import Memleaf
from memleaf.config import save_config
from memleaf.index import event_key
from tests.test_general_evidence_admission import Backend, candidate, summary


class SummaryDateGroundingIntegrationTests(unittest.TestCase):
    def test_external_body_dates_require_source_grounding(self):
        for tool in ('document.read', 'arbitrary_observer'):
            for source, body, writes in (
                ('Finish the review this Wednesday.', 'Finish the review on 2026-09-09.', 0),
                ('Finish the review on 2026-09-09.', 'Finish the review on 2026-09-09.', 1),
                ('Finish the review by 9月3日.', 'Finish the review by 9月3日.', 1),
                ('Finish the review by 9月3日.', 'Finish the review by 2026-09-03.', 0),
            ):
                with self.subTest(tool=tool, source=source, body=body), tempfile.TemporaryDirectory() as root:
                    core = Memleaf(Path(root)/'vault')
                    config = core.vault.config()
                    config['capture'].update(tool_evidence_mode='bounded', include_attachments=False)
                    save_config(core.vault.config_path, config)
                    core.capture('hermes', 's', 't', 'user', 'Review the supplied source.', event_id='u')
                    core.capture('hermes', 's', 't', 'assistant', 'Reviewed.', event_id='a', tool_evidence=[
                        dict(tool_name=tool, call_id='source', kind='external_observation',
                             result_status='success', execution_status='success', completeness='complete',
                             source_type='tool_result', content=source)
                    ])
                    key = event_key('a')
                    backend = Backend([candidate('c', key, source, scope='global')],
                                      {'c':summary(key, body, scope='global')}, semantic=True)
                    result = core.process(model=backend)
                    self.assertEqual(result['memories_written'], writes)
                    if not writes:
                        self.assertEqual(core.vault.list_markdown('knowledge'), [])
                        self.assertEqual(core.vault.list_markdown('history'), [])
                        self.assertGreater(result['deferred_candidates'], 0)
