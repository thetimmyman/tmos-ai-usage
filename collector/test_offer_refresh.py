import json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
import offer_refresh as o

class OfferTest(unittest.TestCase):
    def test_only_explicit_banners_and_timestamp(self):
        now=datetime(2026,9,24,tzinfo=timezone.utc)
        raw=b'<script>DEAL typesafe/jev free</script><div>DEAL <code>stealth/space-bunny-alpha</code> is free. Requests on this model cost no credits.</div><div>DEAL minimax-m3 2x usage.</div> How it works'
        result=o.parse(raw,now)
        self.assertEqual([r['model'] for r in result['offers']],['minimax-m3','stealth/space-bunny-alpha'])
        self.assertNotIn('routing_authorized',result)
        with self.assertRaises(ValueError):o.parse(b'<p>minimax-m3 price table</p>',now)
    def test_failed_refresh_never_renews_evidence(self):
        now=datetime(2026,9,24,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            raw=b'DEAL minimax-m3 2x usage. How it works'
            a=o.load(d,now,lambda:raw)
            def fail():raise OSError('offline')
            b=o.load(d,now+timedelta(days=1),fail)
            self.assertEqual(a['observed_at'],b['observed_at'])
            self.assertEqual((Path(d)/'offer-observations.json').stat().st_mode & 0o777,0o600)

    def test_malformed_recent_cache_is_refetched_before_display(self):
        now=datetime(2026,9,24,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            raw=b'DEAL minimax-m3 2x usage. How it works'
            valid=o.parse(raw,now)
            valid['offers'][0]['source']='https://attacker.invalid'
            (Path(d)/'offer-observations.json').write_text(json.dumps(valid))
            result=o.load(d,now,lambda:raw)
            self.assertEqual(result['source_sha256'],o.hashlib.sha256(raw).hexdigest())
            self.assertEqual(result['offers'][0]['source'],o.URL)
