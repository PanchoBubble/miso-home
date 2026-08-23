"""Transactional SQLite event and memory storage with FTS5 search."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from miso.identity import Actor, VOICE_ACTOR, normalize_email, private_owner

SCHEMA_VERSION = 3

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

MIGRATION_2 = """
CREATE TABLE scheduled_items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('timer', 'reminder')),
    title TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
) STRICT;
CREATE INDEX scheduled_items_due
    ON scheduled_items(status, due_at, kind);

CREATE TABLE shopping_lists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    shared INTEGER NOT NULL DEFAULT 1 CHECK (shared IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE shopping_items (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
    added_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
) STRICT;
CREATE INDEX shopping_items_list_status
    ON shopping_items(list_id, status, completed, created_at);
"""

MIGRATION_3 = """
CREATE TABLE household_members (
    email TEXT PRIMARY KEY COLLATE NOCASE,
    display_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

ALTER TABLE conversations ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'
    CHECK (visibility IN ('shared', 'private'));
ALTER TABLE conversations ADD COLUMN owner_email TEXT REFERENCES household_members(email);
ALTER TABLE conversations ADD COLUMN created_by TEXT NOT NULL DEFAULT 'household:voice';

ALTER TABLE events ADD COLUMN actor_id TEXT NOT NULL DEFAULT 'household:voice';
ALTER TABLE events ADD COLUMN actor_source TEXT NOT NULL DEFAULT 'voice'
    CHECK (actor_source IN ('web', 'voice', 'system'));

ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'
    CHECK (visibility IN ('shared', 'private'));
ALTER TABLE memories ADD COLUMN owner_email TEXT REFERENCES household_members(email);
ALTER TABLE memories ADD COLUMN created_by TEXT NOT NULL DEFAULT 'household:voice';

ALTER TABLE scheduled_items ADD COLUMN visibility TEXT NOT NULL DEFAULT 'shared'
    CHECK (visibility IN ('shared', 'private'));
ALTER TABLE scheduled_items ADD COLUMN owner_email TEXT REFERENCES household_members(email);
ALTER TABLE scheduled_items ADD COLUMN created_by TEXT NOT NULL DEFAULT 'household:voice';

ALTER TABLE shopping_lists ADD COLUMN owner_email TEXT REFERENCES household_members(email);
ALTER TABLE shopping_lists ADD COLUMN created_by TEXT NOT NULL DEFAULT 'household:voice';
ALTER TABLE shopping_items ADD COLUMN actor_id TEXT NOT NULL DEFAULT 'household:voice';
CREATE INDEX conversations_visibility_owner ON conversations(visibility, owner_email);
CREATE INDEX memories_visibility_owner ON memories(visibility, owner_email);
CREATE INDEX scheduled_items_visibility_owner
    ON scheduled_items(visibility, owner_email, status, due_at);
CREATE INDEX shopping_lists_visibility_owner ON shopping_lists(shared, owner_email);
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


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    event_id: int
    conversation_id: str
    kind: str
    role: str | None
    content: str
    payload: Mapping[str, object]
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
            if version < 2:
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{MIGRATION_2}\n"
                    "PRAGMA user_version = 2;\nCOMMIT;"
                )
            if version < 3:
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{MIGRATION_3}\n"
                    "PRAGMA user_version = 3;\nCOMMIT;"
                )

    def integrity_check(self) -> str:
        with self.connect() as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])

    def provision_household_members(self, emails: Iterable[str]) -> None:
        now = utc_now()
        normalized = sorted({normalize_email(email) for email in emails})
        with self.connect() as connection:
            for email in normalized:
                connection.execute(
                    """
                    INSERT INTO household_members(email, created_at, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(email) DO UPDATE SET enabled = 1, updated_at = excluded.updated_at
                    """,
                    (email, now, now),
                )

    def create_conversation(
        self,
        conversation_id: str | None = None,
        *,
        actor: Actor = VOICE_ACTOR,
        visibility: str = "shared",
    ) -> str:
        identifier = conversation_id or str(uuid.uuid4())
        now = utc_now()
        owner = private_owner(actor, visibility)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations(
                    id, created_at, updated_at, visibility, owner_email, created_by
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (identifier, now, now, visibility, owner, actor.actor_id),
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
        actor: Actor = VOICE_ACTOR,
    ) -> int:
        now = utc_now()
        encoded = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            if not self._conversation_accessible(connection, conversation_id, actor):
                raise PermissionError("conversation is not accessible to this actor")
            cursor = connection.execute(
                """
                INSERT INTO events(
                    conversation_id, kind, role, content, payload_json, created_at,
                    actor_id, actor_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id, kind, role, content, encoded, now,
                    actor.actor_id, actor.source,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            return int(cursor.lastrowid)

    def conversation_exists(
        self, conversation_id: str, *, actor: Actor = VOICE_ACTOR
    ) -> bool:
        with self.connect() as connection:
            return self._conversation_accessible(connection, conversation_id, actor)

    def events(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        actor: Actor = VOICE_ACTOR,
    ) -> list[ConversationEvent]:
        bounded_limit = max(1, min(limit, 500))
        with self.connect() as connection:
            if not self._conversation_accessible(connection, conversation_id, actor):
                raise PermissionError("conversation is not accessible to this actor")
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT id, conversation_id, kind, role, content,
                           payload_json, created_at
                    FROM events WHERE conversation_id = ?
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (conversation_id, bounded_limit),
            ).fetchall()
        return [
            ConversationEvent(
                event_id=row["id"],
                conversation_id=row["conversation_id"],
                kind=row["kind"],
                role=row["role"],
                content=row["content"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def add_memory(
        self,
        content: str,
        *,
        kind: str = "explicit",
        importance: float = 0.5,
        source_event_id: int | None = None,
        tags: Iterable[str] = (),
        source_links: Sequence[Mapping[str, str | None]] = (),
        actor: Actor = VOICE_ACTOR,
        visibility: str = "shared",
    ) -> int:
        now = utc_now()
        owner = private_owner(actor, visibility)
        normalized_tags = sorted({tag.strip().casefold() for tag in tags if tag.strip()})
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories(
                    kind, content, importance, source_event_id, created_at, updated_at,
                    visibility, owner_email, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind, content, importance, source_event_id, now, now,
                    visibility, owner, actor.actor_id,
                ),
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

    def search(
        self, query: str, *, limit: int = 20, actor: Actor = VOICE_ACTOR
    ) -> list[SearchResult]:
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
                  AND (m.visibility = 'shared' OR m.owner_email = ?)
                UNION ALL
                SELECT 'event' AS record_type, e.id, e.content,
                       bm25(event_fts) AS rank, e.created_at
                FROM event_fts
                JOIN events AS e ON e.id = event_fts.rowid
                JOIN conversations AS c ON c.id = e.conversation_id
                WHERE event_fts MATCH ?
                  AND (c.visibility = 'shared' OR c.owner_email = ?)
                ORDER BY rank, created_at DESC
                LIMIT ?
                """,
                (query, actor.email, query, actor.email, bounded_limit),
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

    def delete_memory(
        self, memory_id: int, *, actor: Actor = VOICE_ACTOR
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM memories
                WHERE id = ? AND (visibility = 'shared' OR owner_email = ?)
                """,
                (memory_id, actor.email),
            )
            connection.execute(
                "DELETE FROM tags WHERE NOT EXISTS "
                "(SELECT 1 FROM memory_tags WHERE memory_tags.tag_id = tags.id)"
            )
            return cursor.rowcount == 1

    def delete_event(
        self,
        event_id: int,
        *,
        delete_derived: bool = True,
        actor: Actor = VOICE_ACTOR,
    ) -> bool:
        with self.connect() as connection:
            accessible = connection.execute(
                """
                SELECT 1 FROM events AS e
                JOIN conversations AS c ON c.id = e.conversation_id
                WHERE e.id = ? AND (c.visibility = 'shared' OR c.owner_email = ?)
                """,
                (event_id, actor.email),
            ).fetchone()
            if accessible is None:
                return False
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

    @staticmethod
    def _conversation_accessible(
        connection: sqlite3.Connection, conversation_id: str, actor: Actor
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM conversations
            WHERE id = ? AND (visibility = 'shared' OR owner_email = ?)
            """,
            (conversation_id, actor.email),
        ).fetchone()
        return row is not None
