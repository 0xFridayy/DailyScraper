"""Keep private vendor data out of public workflow commit steps."""
from pathlib import Path
import re
import unittest

RETIRED = ('daily-scrape.yml','ownership-capture.yml','price-history-topup.yml',
           'broker-learning-daily.yml','broker-learning-weekly.yml','arb-veto.yml')

class VendorStorageBoundaryTests(unittest.TestCase):
    def test_no_workflow_commits_vendor_databases_or_raw_fragments(self):
        pattern = re.compile(r'git\\s+add[^\\n]*(?:neobdm(?:_ownership)?\\.db|market_summary_raw_fragments|ownership_raw_fragments|daily_picks\\.db|broker_learning\\.db)')
        for path in Path(__file__).with_name('.github').joinpath('workflows').glob('*.yml'):
            with self.subTest(workflow=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding='utf-8')))

    def test_retired_writers_cannot_contact_vendors_or_publish_artifacts(self):
        root=Path(__file__).with_name('.github')/'workflows'
        for name in RETIRED:
            with self.subTest(workflow=name):
                text=(root/name).read_text(encoding='utf-8')
                self.assertNotIn('cron:',text)
                self.assertNotIn('secrets.',text)
                self.assertNotIn('upload-artifact',text)
                self.assertNotIn('contents: write',text)
                self.assertIn('SPECTRA-evidence',text)
                self.assertIn('exit 1',text)

if __name__=='__main__':
    unittest.main()
