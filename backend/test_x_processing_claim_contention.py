from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import sqlite3

from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository


def test_claim_filters_history_and_preserves_stage_routing(tmp_path):
    repo = SQLiteXProcessingRepository(tmp_path / 'tasks.sqlite')
    with repo._connect() as conn:
        conn.execute("INSERT INTO x_capture_accounts(username,username_lower,is_ai_source) VALUES('AI','ai',1)")
        conn.executemany(
            "INSERT INTO tasks(source,source_item_id,content,status,metadata,created_at) VALUES(?,?,?,?,?,?)",
            [
                ('x', 'history', 'old', 'discarded', '{}', '2020-01-01'),
                ('x', 'ai', 'ai', 'pending', '{"account_username":"ai"}', '2020-01-02'),
                ('x', 'crypto', 'crypto', 'pending', '{}', '2020-01-03'),
            ],
        )
    assert repo.claim_task('judge_crypto', worker_id='crypto').source_item_id == 'crypto'
    assert repo.claim_task('judge_ai', worker_id='ai').source_item_id == 'ai'
    assert repo.claim_task('judge_crypto', worker_id='another') is None


def test_concurrent_workers_claim_each_task_at_most_once(tmp_path):
    repo = SQLiteXProcessingRepository(tmp_path / 'tasks.sqlite')
    with repo._connect() as conn:
        conn.execute("INSERT INTO tasks(source,source_item_id,content,status) VALUES('x','one','text','pending')")
    barrier = Barrier(2)

    def claim(worker):
        barrier.wait()
        return repo.claim_task('judge_crypto', worker_id=worker)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ('worker-1', 'worker-2')))
    assert sum(result is not None for result in results) == 1
    with repo._connect() as conn:
        assert conn.execute("SELECT attempt_count FROM tasks WHERE source_item_id='one'").fetchone()[0] == 1


def test_selection_does_not_hold_write_lock_and_rechecks_changed_task(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.sqlite'
    repo = SQLiteXProcessingRepository(path)
    with repo._connect() as conn:
        conn.execute("INSERT INTO tasks(source,source_item_id,content,status) VALUES('x','one','text','pending')")
    selected = Event()
    proceed = Event()
    original = repo._eligible

    def wait_after_read(*args):
        result = original(*args)
        if not selected.is_set():
            selected.set()
            assert proceed.wait(5)
        return result

    monkeypatch.setattr(repo, '_eligible', wait_after_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(repo.claim_task, 'judge_crypto', worker_id='worker')
        assert selected.wait(5)
        with sqlite3.connect(path, timeout=1) as conn:
            conn.execute("UPDATE tasks SET status='discarded' WHERE source_item_id='one'")
        proceed.set()
        assert future.result(timeout=5) is None
