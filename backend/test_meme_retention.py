from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from packages.meme_scanner.scanner import Store
from backend.test_meme_scanner import token, args
from packages.meme_scanner.retention import prune


class RetentionTests(unittest.TestCase):
    def test_history_cleanup_preserves_trigger_identity_and_key_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / 'test.sqlite')
            c = store.conn
            now = datetime(2026, 10, 13, tzinfo=UTC)
            old = (now - timedelta(days=4)).isoformat()
            recent = (now - timedelta(days=2)).isoformat()
            for i, stamp in [(1, old), (2, old), (3, recent)]:
                c.execute("INSERT INTO token_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (i,'coin','bsc','p','S','N',3000000,1000000,stamp,'completed',None,'{}'))
            for level, sid in [(500000,1),(3000000,2)]:
                c.execute("INSERT INTO market_cap_milestones VALUES (?,?,?,?,?,?,?,?,?,?)",
                          ('coin','bsc','p','S',level,old,sid,f'market_cap:bsc:coin:{level}',None,'detected'))
            for key, status, stamp in [('market_cap:bsc:coin:3000000','publisher_pending',old),
                                      ('tg_burst:1','processing',old),('recent','discarded',recent)]:
                c.execute("INSERT INTO jobs(address,trigger_key,payload_json,trigger_kind,queued_at,status,updated_at) VALUES (?,?,?,?,?,?,?)",
                          ('coin',key,'{}','test',old,status,stamp))
            for i in [1,2]:
                c.execute("INSERT INTO tg_candidates(id,address,detected_at,window_start,mention_count,chat_count,sender_count,evidence_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (i,'coin',old,old,5,3,3,'{}',old))
            c.execute("INSERT INTO observations(address,chain,platform,symbol,last_market_cap,highest_market_cap,last_seen_at,tracking_status) VALUES (?,?,?,?,?,?,?,?)",
                      ('coin','bsc','p','S',3000000,3000000,old,'expired'))
            c.execute('CREATE TABLE ca_mentions(id INTEGER PRIMARY KEY, sent_at TEXT)')
            c.executemany('INSERT INTO ca_mentions VALUES (?,?)', [(1,old),(2,recent)])
            c.commit()
            result = prune(c, now)
            self.assertEqual(result['token_snapshots'],1)
            self.assertEqual([r[0] for r in c.execute('SELECT id FROM token_snapshots ORDER BY id')],[2,3])
            self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
            self.assertEqual([r[0] for r in c.execute('SELECT id FROM tg_candidates')],[1])
            self.assertEqual(c.execute('SELECT count(*) FROM key_breakthrough_results').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT snapshot_id FROM market_cap_milestones WHERE level=500000').fetchone()[0],0)
            row = store.observation('coin','bsc')
            self.assertEqual(row['tracking_status'],'expired')
            self.assertEqual(row['highest_market_cap'],3000000)
            self.assertEqual(row['symbol'],'')
            self.assertEqual(c.execute('SELECT count(*) FROM ca_mentions').fetchone()[0],1)
            previous, fresh, status = store.start_or_observe_completed(
                token('coin',4000000,2000000), observed_at=now.isoformat(),
                tracking_window_seconds=259200, args=args(Path(root)))
            self.assertFalse(fresh)
            self.assertEqual(status,'expired')
            self.assertEqual(previous,3000000)
            self.assertFalse(store.record_milestone(token('coin',4000000,2000000),
                level=3000000,observed_at=now.isoformat(),snapshot_id=2))
            self.assertEqual(prune(c,now)['token_snapshots'],0)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            store.close()


if __name__ == '__main__':
    unittest.main()
