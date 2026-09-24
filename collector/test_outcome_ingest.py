import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone
import outcome_ingest as oi

class OutcomeIngestTest(unittest.TestCase):
    def test_refresh_replay_partial_line_privacy_and_absence(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);inbox=p/'outcome-events';inbox.mkdir()
            doc={'providers':[{'provider':'codex'}]}
            now=datetime(2026,9,24,18,tzinfo=timezone.utc)
            oi.attach(doc,p,now);self.assertFalse(doc['providers'][0]['outcomes']['available'])
            event={'type':'task_registered','event_id':'registration-1','task_id':'private-task','provider':'codex','cohort':'fixes','started_at':'2026-09-24T12:00:00Z'}
            f=inbox/'events.jsonl';f.write_text(json.dumps(event)+'\n{"type":')
            self.assertEqual(oi.ingest(p)['applied'],1)
            self.assertEqual(oi.ingest(p)['duplicates'],1)
            oi.attach(doc,p,now)
            self.assertEqual(doc['providers'][0]['outcomes']['counts']['pending'],1)
            self.assertNotIn('private-task',json.dumps(doc))
            (inbox/'bad.jsonl').write_text('not JSON\n')
            self.assertEqual(oi.ingest(p)['rejected_files'],1)
            repaired={'type':'task_registered','event_id':'registration-2','task_id':'recovered-task',
                      'provider':'codex','cohort':'fixes','started_at':'2026-09-24T12:00:00Z'}
            (inbox/'bad.jsonl').write_text(json.dumps(repaired))
            self.assertEqual(oi.ingest(p)['rejected_files'],0)
            self.assertEqual(oi.status(p)['rejected_files'],0)

    def test_fair_rotation_eventually_processes_more_than_batch_limit_and_final_json_without_newline(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); inbox=p/'outcome-events'; inbox.mkdir()
            for i in range(12):
                event={'type':'task_registered','event_id':f'reg-{i}','task_id':f'task-{i}',
                       'provider':'codex','cohort':'fixes','started_at':'2026-09-24T12:00:00Z'}
                (inbox/f'{i:02}.jsonl').write_text(json.dumps(event))
            first=oi.ingest(p)
            self.assertEqual(first['pending_files'],2)
            self.assertEqual(oi.status(p)['unresolved_files'],2)
            second=oi.ingest(p)
            self.assertEqual(second['pending_files'],0)
            self.assertEqual(oi.status(p)['unresolved_files'],0)
            summary=oi.outcome_ledger.summarize_all(p/'outcome-ledger.json','codex',
                '2026-09-24T00:00:00Z','2026-09-25T00:00:00Z')
            self.assertEqual(summary['counts']['tasks'],12)

    def test_rejected_fifo_symlink_and_oversized_exports_are_never_unbounded_read(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); inbox=p/'outcome-events'; inbox.mkdir()
            os.mkfifo(inbox/'fifo.jsonl')
            target=p/'target'; target.write_text('not an event')
            (inbox/'link.jsonl').symlink_to(target)
            (inbox/'large.jsonl').write_bytes(b' ' * (oi.MAX_BYTES + 1))
            result=oi.ingest(p)
            self.assertEqual(result['rejected_files'],3)
            self.assertEqual(oi.status(p)['unresolved_files'],3)

    def test_missing_ledger_is_unresolved_and_rebuilt_from_retained_export(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d); inbox=p/'outcome-events'; inbox.mkdir()
            event={'type':'task_registered','event_id':'registration-1','task_id':'private-task',
                   'provider':'codex','cohort':'fixes','started_at':'2026-09-24T12:00:00Z'}
            (inbox/'events.jsonl').write_text(json.dumps(event))
            self.assertEqual(oi.ingest(p)['applied'],1)
            ledger=p/'outcome-ledger.json'; ledger.unlink()
            self.assertGreater(oi.status(p)['unresolved_files'],0)
            self.assertEqual(oi.ingest(p)['applied'],1)
            self.assertEqual(oi.status(p)['unresolved_files'],0)
