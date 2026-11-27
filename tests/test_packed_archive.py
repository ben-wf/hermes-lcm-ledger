from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Barrier

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.engine import LCMEngine
from hermes_lcm.packed_archive import pack_messages
from hermes_lcm.store import MessageStore
from scripts.pack_archive import convert

BODY = "Shared tool result with searchable lighthouse. " * 25


def engine(path):
    instance = LCMEngine(config=LCMConfig(database_path=str(path)), hermes_home=str(path.parent))
    instance.on_session_start("shared-session", platform="telegram", conversation_id="test")
    return instance


@pytest.mark.parametrize("packed", [False, True])
def test_independent_writers_reconcile_and_preserve_repeated_occurrences(tmp_path, packed):
    path = tmp_path / "ledger.db"
    first = engine(path)
    if packed:
        first._close_storage()
        with closing(sqlite3.connect(path)) as conn:
            pack_messages(conn)
        first = engine(path)
    second = engine(path)
    history = [{"role": "user", "content": "look"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "call", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "call", "content": BODY}]
    barrier = Barrier(2)
    def ingest(instance):
        barrier.wait(timeout=10)
        instance.ingest(history)
    try:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(ingest, (first, second)))
        assert first._store.get_session_count("shared-session") == 3
        first.ingest(history + history)
        second.ingest(history + history)
        assert second._store.get_session_count("shared-session") == 6
        rows = second._store.get_session_messages("shared-session")
        assert [row["content"] for row in rows if row["role"] == "tool"] == [BODY, BODY]
    finally:
        first._close_storage()
        second._close_storage()


def test_packed_writes_search_and_last_reference_cleanup(tmp_path):
    source, packed = (tmp_path / name for name in ("source.db", "packed.db"))
    original = MessageStore(source)
    a = original.append("a", {"role": "tool", "content": BODY})
    original.close()
    convert(source, packed)
    with pytest.raises(FileExistsError):
        convert(source, packed)
    store = MessageStore(packed)
    try:
        b, c = store.append_batch("b", [{"role": "tool", "content": BODY},
                                       {"role": "tool", "content": "another " + BODY}])
        assert a < b < c
        assert store.get(a)["content"] == store.get(b)["content"] == BODY
        assert store.connection.execute("SELECT count(*) FROM lcm_tool_payloads").fetchone()[0] == 2
        assert store.reassign_session_messages("b", "c") == 2
        store.pin(b)
        assert not store.gc_externalized_tool_result(b, "cleaned")
        store.unpin(b)
        assert store.gc_externalized_tool_result(b, "cleaned")
        assert store.get(b)["content"] == "cleaned"
        assert store.get(a)["content"] == BODY
        assert store.delete_session_messages("c") == 2
        matches = store.connection.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'lighthouse'")
        assert matches.fetchall() == [(a,)]
        assert store.connection.execute("SELECT count(*) FROM lcm_tool_payloads").fetchone()[0] == 1
        assert store.delete_session_messages("a") == 1
        assert store.connection.execute("SELECT count(*) FROM lcm_tool_payloads").fetchone()[0] == 0
        store.connection.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES('integrity-check',1)")
    finally:
        store.close()


def test_cross_process_ingest_after_both_engines_bind(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from tempfile import TemporaryFile

    path = tmp_path / "ledger.db"
    repo = Path(__file__).resolve().parents[1]
    # Fresh interpreters each load the plugin independently, as CLI and gateway do.
    worker = """
import sys, types
pkg = types.ModuleType('hermes_lcm')
pkg.__path__ = [sys.argv[1]]
sys.modules['hermes_lcm'] = pkg
from hermes_lcm.engine import LCMEngine
from hermes_lcm.config import LCMConfig
e = LCMEngine(config=LCMConfig(database_path=sys.argv[2]), hermes_home=sys.argv[3])
e.on_session_start('shared-session', platform='telegram')
print('ready', flush=True)
if sys.stdin.readline().strip() == 'go':
    e.ingest([{'role':'user', 'content':'lookup'},
              {'role':'assistant','content':'checking'},
              {'role':'tool','tool_call_id':'call-1','content':'result'}])
e._close_storage()
"""
    with TemporaryFile() as errors:
        children = []
        try:
            for _ in range(2):
                child = subprocess.Popen(
                    [sys.executable, "-c", worker, str(repo), str(path), str(tmp_path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True,
                )
                children.append(child)
                assert child.stdout.readline().strip() == "ready"
            for child in children:
                child.stdin.write("go\n")
                child.stdin.flush()
            for child in children:
                assert child.wait(timeout=30) == 0
        finally:
            for child in children:
                child.kill()
                child.wait()
                child.stdin.close()
                child.stdout.close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT role FROM messages ORDER BY store_id").fetchall() == [
            ("user",), ("assistant",), ("tool",),
        ]
