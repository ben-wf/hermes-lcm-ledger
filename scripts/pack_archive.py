"""Create and verify a separate, compact Ledger database from a SQLite snapshot."""

import argparse
from contextlib import closing
from itertools import zip_longest
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packed_archive import pack_messages  # noqa: E402


def verify(source, packed):
    for table, key in (("messages", "store_id"), ("summary_nodes", "node_id")):
        if not source.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (table,)).fetchone():
            continue
        read = f"SELECT * FROM {table} ORDER BY {key}"
        for before, after in zip_longest(source.execute(read), packed.execute(read)):
            if before != after or any(type(x) is not type(y) for x, y in zip(before, after)):
                raise ValueError(f"{table} differs after conversion")
    if packed.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("Converted database failed SQLite quick_check")
    if packed.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Converted database contains a broken foreign key")
    packed.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES('integrity-check',1)")
    packed.rollback()


def convert(source_path, output_path):
    if output_path.exists():
        raise FileExistsError(output_path)
    with TemporaryDirectory(prefix=".ledger-pack-", dir=output_path.parent) as directory:
        work = Path(directory) / "work.db"
        result = Path(directory) / "compact.db"
        with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            source.execute("BEGIN")
            source.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            with closing(sqlite3.connect(work)) as conn:
                source.backup(conn, pages=1024)
                conn.execute("PRAGMA journal_mode=DELETE")
                pack_messages(conn)
                conn.execute("VACUUM INTO ?", (str(result),))
            with closing(sqlite3.connect(result)) as packed:
                verify(source, packed)
        result.chmod(0o600)
        os.link(result, output_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    convert(args.source, args.output)
    print(f"Verified archive: {args.output} ({args.output.stat().st_size:,} bytes)")
