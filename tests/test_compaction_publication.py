"""Regression coverage for leaf checkpoints and large summary graphs."""

import json
import sqlite3

import pytest

from hermes_lcm.config import LCMConfig
from hermes_lcm.dag import SummaryDAG, SummaryNode
from hermes_lcm.engine import LCMEngine
from hermes_lcm.rollup_store import RollupStore


@pytest.fixture
def bound_engine(tmp_path):
    config = LCMConfig(
        database_path=str(tmp_path / "archive.db"),
        fresh_tail_count=2,
        leaf_chunk_tokens=1,
    )
    engine = LCMEngine(config=config)
    try:
        engine.on_session_start("session", platform="cli", conversation_id="conversation")
        yield engine
    finally:
        engine.shutdown()


def test_whole_session_depths_drive_context_and_condensation(bound_engine, monkeypatch):
    engine = bound_engine
    with sqlite3.connect(engine._dag.db_path) as seed:
        seed.executemany(
            """INSERT INTO summary_nodes(session_id, depth, summary, created_at)
               VALUES ('session', 0, 'covered leaf', ?)""",
            ((index,) for index in range(1000)),
        )
        leaves = [row[0] for row in seed.execute("SELECT node_id FROM summary_nodes ORDER BY node_id")]
        seed.executemany(
            """INSERT INTO summary_nodes(session_id, depth, source_type, source_ids, summary, created_at)
               VALUES ('session', 1, 'nodes', ?, ?, ?)""",
            ((json.dumps(chunk), f"session arc {index}", index)
             for index, chunk in enumerate((leaves[:500], leaves[500:]))),
        )

    assert [n.summary for n in engine._summary_frontier_nodes()] == ["session arc 0", "session arc 1"]
    context = engine._assemble_context(None, [])
    assert "session arc 0" in context[0]["content"]
    assert "session arc 1" in context[0]["content"]
    assert "covered leaf" not in context[0]["content"]

    engine._config.incremental_max_depth = -1
    engine._config.condensation_fanin = 2
    monkeypatch.setattr("hermes_lcm.engine.summarize_with_escalation", lambda **kwargs: ("combined", 1))
    engine._maybe_condense()
    assert [(n.depth, n.summary) for n in engine._summary_frontier_nodes()] == [(2, "combined")]


def test_failed_node_insert_rolls_back_frontier_and_rejects_stale_writer(bound_engine):
    engine = bound_engine
    first = engine._dag
    second = SummaryDAG(first.db_path)
    leaf = SummaryNode(session_id="session", summary="atomic leaf", source_ids=[1, 2, 3])
    checkpoint = dict(conversation_id="conversation", expected_frontier=0, next_frontier=3)
    try:
        first._conn.execute(
            """CREATE TEMP TRIGGER fail_leaf BEFORE INSERT ON summary_nodes
               BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected publication failure"):
            first.add_node(leaf, **checkpoint)
        assert leaf.node_id == 0
        assert second.get_session_node_count("session") == 0
        assert engine._lifecycle.get_by_conversation("conversation").current_frontier_store_id == 0

        first._conn.execute("DROP TRIGGER fail_leaf")
        assert first.add_node(leaf, **checkpoint) == leaf.node_id > 0
        stale = SummaryNode(session_id="session", summary="stale leaf", source_ids=[1, 2, 3, 4])
        assert second.add_node(stale, **{**checkpoint, "next_frontier": 4}) == 0
        assert stale.node_id == 0
        other = SummaryNode(session_id="other", summary="wrong binding", source_ids=[4])
        assert second.add_node(
            other, conversation_id="conversation", expected_frontier=3, next_frontier=4,
        ) == 0
        assert second.get_session_node_count("other") == 0
        assert [n.summary for n in second.get_session_nodes("session")] == ["atomic leaf"]
        assert engine._lifecycle.get_by_conversation("conversation").current_frontier_store_id == 3
    finally:
        second.close()


def test_post_publication_failure_and_restart_do_not_duplicate_leaf(bound_engine, monkeypatch):
    engine = bound_engine
    messages = [{"role": "user", "content": f"original turn {idx}"} for idx in range(8)]
    monkeypatch.setattr("hermes_lcm.engine.summarize_with_escalation", lambda **kwargs: ("summary", 1))
    map_calls = []
    match = engine._get_store_ids_for_messages

    def record_mapping(chunk):
        map_calls.append(len(chunk))
        return match(chunk)

    def fail_after_publication(*args):
        raise RuntimeError("post-publication failure")

    monkeypatch.setattr(engine, "_get_store_ids_for_messages", record_mapping)
    engine._config.temporal_rollups_enabled = True
    with_rollups = RollupStore(engine._dag.db_path)
    with_rollups.close()
    monkeypatch.setattr(engine, "_invalidate_rollups_for_published_node", fail_after_publication)
    with pytest.raises(RuntimeError, match="post-publication failure"):
        engine.compress(messages)
    nodes = engine._dag.get_session_nodes("session")
    assert len(nodes) == 1
    assert len(nodes[0].source_ids) == 6
    assert engine._dag.connection.execute(
        "SELECT node_id FROM lcm_rollup_invalidations WHERE scope='session'"
    ).fetchall() == [(nodes[0].node_id,)]
    assert engine._lifecycle.get_by_conversation("conversation").current_frontier_store_id == max(nodes[0].source_ids)
    assert map_calls.count(6) == 1

    restarted = LCMEngine(config=engine._config)
    try:
        monkeypatch.setattr(restarted, "_schedule_rollup_maintenance", lambda *_: None)
        restarted.on_session_start("session", platform="cli", conversation_id="conversation")
        monkeypatch.setattr(
            "hermes_lcm.engine.summarize_with_escalation",
            lambda **kwargs: pytest.fail("a replayed leaf must not spend a second summary call"),
        )
        replay = restarted.compress(messages)
        assert "summary" in replay[0]["content"]
        assert not restarted._dag.connection.execute(
            "SELECT 1 FROM lcm_rollup_invalidations WHERE scope='session'"
        ).fetchall()
        assert restarted._dag.get_session_nodes("session") == nodes
        assert restarted._last_compacted_store_id == max(nodes[0].source_ids)
    finally:
        restarted.shutdown()


def test_replayed_inherited_prefix_still_compacts_following_local_rows(bound_engine, monkeypatch):
    engine = bound_engine
    engine.threshold_tokens = 30
    engine._config.threshold_full_sweep_enabled = True
    engine._config.summary_prefix_target_tokens = 1000
    monkeypatch.setattr(engine, "_select_oldest_leaf_chunk", lambda messages, _: messages[:2])

    inherited = [{"role": "user", "content": f"inherited {index}"} for index in range(2)]
    local = [{"role": "user", "content": f"local {index}"} for index in range(4)]
    source_ids = engine._store.append_batch("session", local, [4] * len(local))
    # The inherited prefix belongs to a prior host session. The host hands it
    # back unchanged on retry, together with newly archived raw messages.
    monkeypatch.setattr(engine, "_ingest_messages", lambda messages: messages)
    calls = []

    def summarize(**kwargs):
        calls.append(kwargs["text"])
        return (f"summary {len(calls)}", 1)

    monkeypatch.setattr("hermes_lcm.engine.summarize_with_escalation", summarize)
    engine.compress(inherited + local[:2], current_tokens=31)
    assert [n.source_ids for n in engine._dag.get_session_nodes("session")] == [[]]
    assert engine._last_compacted_store_id == 0

    replay = engine.compress(inherited + local, current_tokens=31)
    nodes = engine._dag.get_session_nodes("session")
    assert [n.source_ids for n in nodes] == [[], source_ids[:2]]
    assert len(calls) == 2
    assert "local 0" in calls[-1]
    assert "inherited" not in calls[-1]
    assert "summary 1" in replay[0]["content"]
    assert "summary 2" in replay[0]["content"]
    assert engine._last_compacted_store_id == source_ids[1]


def test_source_free_receipt_is_scoped_and_real_new_rows_win(bound_engine):
    dag = bound_engine._dag
    contender = SummaryDAG(dag.db_path)
    try:
        def publish(writer, content, consumed):
            return writer.add_node(
                SummaryNode(session_id="session", summary=content, source_ids=[consumed] if consumed else []),
                conversation_id="conversation",
                input_digest="same-input",
                expected_frontier=0,
                next_frontier=consumed,
            )

        first_id = publish(dag, "inherited", 0)
        assert first_id > 0
        assert publish(contender, "other writer's wording", 0) == first_id
        assert publish(contender, "same text backed by new raw data", 1) > first_id
        assert [n.summary for n in dag.get_session_nodes("session")] == [
            "inherited", "same text backed by new raw data",
        ]
        assert dag.get_leaf_for_input("other", "same-input") is None
        dag.delete_session_nodes("session")
        assert contender.connection.execute("SELECT COUNT(*) FROM lcm_leaf_publications").fetchone() == (0,)
    finally:
        contender.close()
