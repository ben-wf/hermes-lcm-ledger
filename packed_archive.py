"""Optional lossless storage for large tool responses in a Ledger archive."""

import hashlib
import sqlite3

PACKED_VERSION = 6
ROWS = "lcm_message_rows"
MIN_PAYLOAD_BYTES = 256


def is_packed(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT count(*) FROM sqlite_schema WHERE (type='view' AND name='messages') "
        "OR (type='table' AND name IN (?, 'lcm_tool_payloads'))", (ROWS,)
    ).fetchone()[0] == 3


def physical_messages(conn: sqlite3.Connection) -> str:
    return ROWS if is_packed(conn) else "messages"


def shared_content(conn: sqlite3.Connection, role: str, content: str | None) -> tuple[str | None, int | None]:
    """Return inline text or a pool id. The writer owns the transaction."""
    if role != "tool" or not isinstance(content, str):
        return content, None
    data = content.encode("utf-8")
    if len(data) < MIN_PAYLOAD_BYTES:
        return content, None
    digest = hashlib.sha256(data).digest()
    found = conn.execute(
        "SELECT id FROM lcm_tool_payloads WHERE digest=? AND body=? COLLATE BINARY",
        (digest, content),
    ).fetchone()
    if found:
        return None, found[0]
    cursor = conn.execute(
        "INSERT INTO lcm_tool_payloads(digest,body) VALUES (?,?)", (digest, content)
    )
    return None, cursor.lastrowid


def logical_content(row: str) -> str:
    return (
        f"CASE WHEN {row}.payload_id IS NULL THEN {row}.content ELSE "
        f"(SELECT body FROM lcm_tool_payloads WHERE id={row}.payload_id) END"
    )


def packed_fts_triggers() -> tuple[str, ...]:
    old, new = logical_content("old"), logical_content("new")
    remove = f"INSERT INTO messages_fts(messages_fts,rowid,content) VALUES('delete',old.store_id,{old});"
    add = f"INSERT INTO messages_fts(rowid,content) VALUES(new.store_id,{new});"
    # Read the old body before orphan cleanup can remove it.
    return (
        f"CREATE TRIGGER IF NOT EXISTS msg_fts_insert AFTER INSERT ON {ROWS} BEGIN {add} END",
        f"CREATE TRIGGER IF NOT EXISTS msg_fts_delete BEFORE DELETE ON {ROWS} BEGIN {remove} END",
        f"CREATE TRIGGER IF NOT EXISTS msg_fts_update BEFORE UPDATE OF content,payload_id ON {ROWS} "
        f"BEGIN {remove} {add} END",
    )


def pack_messages(conn: sqlite3.Connection) -> None:
    """Convert an isolated v5 copy in one transaction, retaining FTS postings and ids."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if is_packed(conn):
            raise ValueError("Archive already uses shared tool responses")
        version = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if not version or int(version[0]) != 5:
            raise ValueError("Open the archive with Ledger before converting its v5 schema")
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='messages'"
        )}
        if triggers != {"msg_fts_insert", "msg_fts_delete", "msg_fts_update"}:
            raise ValueError("Unexpected message triggers. Check the archive before conversion.")
        for name in sorted(triggers):
            conn.execute(f"DROP TRIGGER {name}")
        columns = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        if not all(name.isidentifier() for name in columns):
            raise ValueError("Unexpected message column")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute(f"ALTER TABLE messages RENAME TO {ROWS}")
        conn.execute("CREATE TABLE lcm_tool_payloads(id INTEGER PRIMARY KEY,digest BLOB NOT NULL,body TEXT NOT NULL)")
        conn.execute("CREATE INDEX lcm_payload_digest ON lcm_tool_payloads(digest)")
        conn.execute(f"ALTER TABLE {ROWS} ADD COLUMN payload_id INTEGER REFERENCES lcm_tool_payloads(id)")
        select = ", ".join(logical_content("m") + " AS content" if n == "content" else f'm."{n}"' for n in columns)
        conn.execute(f"CREATE VIEW messages AS SELECT {select} FROM {ROWS} AS m")
        # Keyset pagination avoids changing a table underneath an open scan.
        last_id = conn.execute(f"SELECT min(store_id) FROM {ROWS}").fetchone()[0]
        if last_id is not None:
            last_id -= 1
            while batch := conn.execute(
                f"SELECT store_id,content FROM {ROWS} WHERE store_id>? AND role='tool' "
                "AND typeof(content)='text' AND length(CAST(content AS BLOB))>=? ORDER BY store_id LIMIT 128",
                (last_id, MIN_PAYLOAD_BYTES),
            ).fetchall():
                for store_id, body in batch:
                    _, payload_id = shared_content(conn, "tool", body)
                    conn.execute(f"UPDATE {ROWS} SET content=NULL,payload_id=? WHERE store_id=?", (payload_id, store_id))
                last_id = batch[-1][0]
        conn.execute(f"CREATE INDEX lcm_payload_members ON {ROWS}(payload_id) WHERE payload_id IS NOT NULL")
        for operation, event in (("delete", "DELETE"), ("update", "UPDATE OF payload_id")):
            conn.execute(
                f"CREATE TRIGGER lcm_payload_gc_{operation} AFTER {event} ON {ROWS} "
                "WHEN old.payload_id IS NOT NULL BEGIN DELETE FROM lcm_tool_payloads WHERE id=old.payload_id "
                f"AND NOT EXISTS (SELECT 1 FROM {ROWS} WHERE payload_id=old.payload_id); END"
            )
        for sql in packed_fts_triggers():
            conn.execute(sql)
        conn.execute("UPDATE metadata SET value=? WHERE key='schema_version'", (str(PACKED_VERSION),))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
