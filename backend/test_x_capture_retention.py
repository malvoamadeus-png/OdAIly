from datetime import UTC, datetime, timedelta

from packages.x_capture.sqlite_repository import SQLiteXCaptureRepository


def test_expired_success_is_removed_but_failures_and_recent_success_are_preserved(tmp_path):
    repo = SQLiteXCaptureRepository(tmp_path / 'capture.sqlite')
    now = datetime.now(UTC)
    old = (now - timedelta(days=2)).isoformat()
    recent = (now - timedelta(hours=12)).isoformat()
    with repo._connect() as conn:
        conn.execute("INSERT INTO x_capture_accounts(username,username_lower) VALUES('alice','alice')")
        account_id = conn.execute('SELECT id FROM x_capture_accounts').fetchone()[0]
        conn.executemany(
            "INSERT INTO x_capture_attempts(account_id,username_lower,status,source,started_at,finished_at,error) VALUES(?, 'alice', ?, 'fxtwitter', ?, ?, ?)",
            [(account_id, 'success', old, old, None), (account_id, 'fetch_failed', old, old, 'network error'),
             (account_id, 'success', recent, recent, None), (account_id, 'success', old, old, 'error')],
        )
    assert repo.prune_attempts_before(now - timedelta(days=1)) == 1
    with repo._connect() as conn:
        assert [tuple(row) for row in conn.execute('SELECT status,error FROM x_capture_attempts ORDER BY id')] == [
            ('fetch_failed', 'network error'), ('success', None), ('success', 'error')
        ]
