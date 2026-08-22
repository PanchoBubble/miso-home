"""Transactional SQLite event and memory storage with FTS5 search."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

SCHEMA_VERSION = 1

MIGRATION_1 = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    role TEXT,
    content TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX events_conversation_created
    ON events(conversation_id, created_at, id);

CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('explicit', 'inferred', 'routine', 'summary')),
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
    source_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE
) STRICT;

CREATE TABLE memory_tags (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, tag_id)
) STRICT;

CREATE TABLE source_links (
    id INTEGER PRIMARY KEY,
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    uri TEXT,
    UNIQUE (memory_id, source_type, source_id)
) STRICT;

CREATE VIRTUAL TABLE event_fts USING fts5(
    content,
    content='events',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
    INSERT INTO event_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
    INSERT INTO event_fts(event_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER events_au AFTER UPDATE OF content ON events BEGIN
    INSERT INTO event_fts(event_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO event_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE VIRTUAL TABLE memory_fts USING fts5(
    content,
    content='memories',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER memories_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO memory_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class SearchResult:
    record_type: str
    record_id: int
    content: str
    rank: float
    created_at: str


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 1:
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{MIGRATION_1}\n"
                    "PRAGMA user_version = 1;\nCOMMIT;"
                )

    def integrity_check(self) -> str:
        with self.connect() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    def create_conversation(self, conversation_id: str | None = None) -> str:
        identifier = conversation_id or str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO conversations(id, created_at, updated_at) VALUES (?, ?, ?)",
                (identifier, now, now),
            )
        return identifier

    def append_event(
        self,
        conversation_id: str,
        *,
        kind: str,
        content: str = "",
        role: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> int:
        now = utc_now()
        encoded = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events(conversation_id, kind, role, content, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (conversation_id, kind, role, content, encoded, now),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            return int(cursor.lastrowid)

    def add_memory(
        self,
        content: str,
        *,
        kind: str = "explicit",
        importance: float = 0.5,
        source_event_id: int | None = None,
        tags: Iterable[str] = (),
        source_links: Sequence[Mapping[str, str | None]] = (),
    ) -> int:
        now = utc_now()
        normalized_tags = sorted({tag.strip().casefold() for tag in tags if tag.strip()})
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories(kind, content, importance, source_event_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (kind, content, importance, source_event_id, now, now),
            )
            memory_id = int(cursor.lastrowid)
            for tag in normalized_tags:
                connection.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
                connection.execute(
                    """
                    INSERT INTO memory_tags(memory_id, tag_id)
                    SELECT ?, id FROM tags WHERE name = ?
                    """,
                    (memory_id, tag),
                )
            for link in source_links:
                connection.execute(
                    """
                    INSERT INTO source_links(memory_id, source_type, source_id, uri)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        link["source_type"],
                        link["source_id"],
                        link.get("uri"),
                    ),
                )
            return memory_id

    def search(self, query: str, *, limit: int = 20) -> list[SearchResult]:
        if not query.strip():
            return []
        bounded_limit = max(1, min(limit, 100))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT 'memory' AS record_type, m.id, m.content,
                       bm25(memory_fts) AS rank, m.created_at
                FROM memory_fts
                JOIN memories AS m ON m.id = memory_fts.rowid
                WHERE memory_fts MATCH ?
                UNION ALL
                SELECT 'event' AS record_type, e.id, e.content,
                       bm25(event_fts) AS rank, e.created_at
                FROM event_fts
                JOIN events AS e ON e.id = event_fts.rowid
                WHERE event_fts MATCH ?
                ORDER BY rank, created_at DESC
                LIMIT ?
                """,
                (query, query, bounded_limit),
            ).fetchall()
        return [
            SearchResult(
                record_type=row["record_type"],
                record_id=row["id"],
                content=row["content"],
                rank=row["rank"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def delete_memory(self, memory_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            connection.execute(
                "DELETE FROM tags WHERE NOT EXISTS "
                "(SELECT 1 FROM memory_tags WHERE memory_tags.tag_id = tags.id)"
            )
            return cursor.rowcount == 1

    def delete_event(self, event_id: int, *, delete_derived: bool = True) -> bool:
        with self.connect() as connection:
            if delete_derived:
                connection.execute(
                    "DELETE FROM memories WHERE source_event_id = ?", (event_id,)
                )
            cursor = connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            connection.execute(
                "DELETE FROM tags WHERE NOT EXISTS "
                "(SELECT 1 FROM memory_tags WHERE memory_tags.tag_id = tags.id)"
            )
            return cursor.rowcount == 1
