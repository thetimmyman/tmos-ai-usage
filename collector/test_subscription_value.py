import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from datetime import datetime, timezone
import subscription_value as v

class SubscriptionValueTest(unittest.TestCase):
    def setUp(self):
        self.pi_patch = patch('pi_activity.scan', return_value={})
        self.pi_patch.start()
        self.addCleanup(self.pi_patch.stop)

    def test_annual_price_and_override(self):
        with tempfile.TemporaryDirectory() as d:
            v.save_price(d, 'opencode-go', 96, 'year')
            doc={'providers':[{'provider':'opencode-go','plan':'OpenCode Go','stats':{'available':True,'coverage_days':30,'totals':{'prompts':40}}}]}
            v.attach(doc,d)
            a=doc['providers'][0]['activity']
            self.assertEqual(a['price']['monthly_usd'],8)
            self.assertFalse(a['price']['estimated'])
            self.assertEqual(a['observed_turns'],40)
            self.assertNotIn('validated_tasks',a)
            self.assertEqual(Path(d,'subscriptions.json').stat().st_mode & 0o777,0o600)
    def test_reference_exact_tier_expiration(self):
        now=datetime(2026,9,24,19,tzinfo=timezone.utc)
        self.assertEqual(v.price_for({'provider':'claude-code','plan_tier':'default_claude_max_20x'}, {},now)['monthly_usd'],200)
        self.assertIsNone(v.price_for({'provider':'claude-code','plan':'max'}, {},now))
        self.assertIsNone(v.price_for({'provider':'opencode-go','plan':'OpenCode Go'}, {},datetime(2027,1,1,tzinfo=timezone.utc)))
    def test_unknown_is_not_zero_and_invalid_amount(self):
        with tempfile.TemporaryDirectory() as d:
            doc={'providers':[{'provider':'codex','stats':{'available':False}}]}
            v.attach(doc,d)
            self.assertIsNone(doc['providers'][0]['activity']['price'])
            self.assertIsNone(doc['providers'][0]['activity']['observed_turns'])
            for bad in [-1,float('nan'),float('inf')]:
                with self.assertRaises(ValueError):v.save_price(d,'codex',bad,'month')
    def test_invalid_config_does_not_claim_reference(self):
        now=datetime(2026,9,24,19,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            Path(d,'subscriptions.json').write_text('{"opencode-go":{"amount_usd":true,"cycle":"month"}}')
            doc={'providers':[{'provider':'opencode-go','plan':'OpenCode Go'}]}
            v.attach(doc,d,now)
            self.assertIsNone(doc['providers'][0]['activity']['price'])
