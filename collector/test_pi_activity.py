import json
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
import pi_activity

class PiActivityTest(unittest.TestCase):
    def test_attribution_dedupe_and_unknown_provider(self):
        with tempfile.TemporaryDirectory() as d:
            rows=[{'type':'session','id':'one'}]
            def msg(id,role,provider=None):return {'id':id,'type':'message','timestamp':'2026-09-24T12:00:00Z','message':{'role':role,'provider':provider,'content':'SECRET never projected'}}
            rows += [msg('u','user'),msg('a','assistant','commandcode'),msg('b','assistant','commandcode'),msg('v','user'),msg('c','assistant','openrouter')]
            text=''.join(json.dumps(r)+'\n' for r in rows)
            Path(d,'one.jsonl').write_text(text);Path(d,'copy.jsonl').write_text(text)
            out=pi_activity.scan(d,datetime(2026,9,24,13,tzinfo=timezone.utc))
            self.assertEqual(out,{'command-code':{'turns':1,'sessions':1}})
            self.assertNotIn('SECRET',json.dumps(out))

    def test_damaged_utf8_does_not_abort_collection(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'damaged.jsonl').write_bytes(b'\xff\xfe\n')
            self.assertEqual(pi_activity.scan(d), {})
