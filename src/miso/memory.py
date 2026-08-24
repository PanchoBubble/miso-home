"""Transactional SQLite event and memory storage with FTS5 search."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from miso.identity import Actor, VOICE_ACTOR, normalize_email, private_owner

SCHEMA_VERSION = 4

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

MIGRATION_4 = """
CREATE TABLE memory_embeddings (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (memory_id, model)
) STRICT;
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
    kind: str
    role: str | None
    conversation_id: str | None
    importance: float | None
    tags: tuple[str, ...]
    source_event_id: int | None
    sources: tuple[Mapping[str, object], ...]
    visibility: str
    created_by: str


@dataclass(frozen=True, slots=True)
class DeletionResult:
    records_deleted: int
    derived_memories_deleted: int
    embeddings_deleted: int


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
            if version < 4:
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{MIGRATION_4}\n"
                    "PRAGMA user_version = 4;\nCOMMIT;"
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
        self,
        query: str,
        *,
        limit: int | None = 20,
        actor: Actor = VOICE_ACTOR,
        kinds: Iterable[str] = (),
        tag: str | None = None,
        record_types: Iterable[str] = ("memory", "event"),
    ) -> list[SearchResult]:
        normalized_query = query.strip()
        normalized_kinds = sorted({kind.strip() for kind in kinds if kind.strip()})
        normalized_tag = tag.strip().casefold() if tag and tag.strip() else None
        requested_types = set(record_types)
        if not requested_types <= {"memory", "event"}:
            raise ValueError("record type must be memory or event")
        bounded_limit = None if limit is None else max(1, min(limit, 5000))
        with self.connect() as connection:
            rows: list[sqlite3.Row] = []
            if "memory" in requested_types:
                if normalized_query:
                    memory_sql = """
                        SELECT 'memory' AS record_type, m.*,
                               bm25(memory_fts) AS rank,
                               NULL AS role, NULL AS conversation_id,
                               NULL AS actor_id, NULL AS actor_source
                        FROM memory_fts
                        JOIN memories AS m ON m.id = memory_fts.rowid
                        WHERE memory_fts MATCH ?
                          AND (m.visibility = 'shared' OR m.owner_email = ?)
                    """
                    memory_parameters: list[object] = [normalized_query, actor.email]
                else:
                    memory_sql = """
                        SELECT 'memory' AS record_type, m.*, 0.0 AS rank,
                               NULL AS role, NULL AS conversation_id,
                               NULL AS actor_id, NULL AS actor_source
                        FROM memories AS m
                        WHERE (m.visibility = 'shared' OR m.owner_email = ?)
                    """
                    memory_parameters = [actor.email]
                if normalized_kinds:
                    placeholders = ",".join("?" for _ in normalized_kinds)
                    memory_sql += f" AND m.kind IN ({placeholders})"
                    memory_parameters.extend(normalized_kinds)
                if normalized_tag:
                    memory_sql += """
                        AND EXISTS (
                            SELECT 1 FROM memory_tags AS mt
                            JOIN tags AS t ON t.id = mt.tag_id
                            WHERE mt.memory_id = m.id AND t.name = ? COLLATE NOCASE
                        )
                    """
                    memory_parameters.append(normalized_tag)
                rows.extend(connection.execute(memory_sql, memory_parameters).fetchall())

            if (
                "event" in requested_types
                and not normalized_kinds
                and normalized_tag is None
            ):
                if normalized_query:
                    event_sql = """
                        SELECT 'event' AS record_type, e.id, e.content,
                               bm25(event_fts) AS rank, e.created_at,
                               e.kind, e.role, e.conversation_id, e.actor_id,
                               e.actor_source, c.visibility, c.created_by,
                               NULL AS importance, NULL AS source_event_id,
                               NULL AS updated_at, NULL AS owner_email
                        FROM event_fts
                        JOIN events AS e ON e.id = event_fts.rowid
                        JOIN conversations AS c ON c.id = e.conversation_id
                        WHERE event_fts MATCH ?
                          AND (c.visibility = 'shared' OR c.owner_email = ?)
                    """
                    event_parameters = (normalized_query, actor.email)
                else:
                    event_sql = """
                        SELECT 'event' AS record_type, e.id, e.content,
                               0.0 AS rank, e.created_at, e.kind, e.role,
                               e.conversation_id, e.actor_id, e.actor_source,
                               c.visibility, c.created_by, NULL AS importance,
                               NULL AS source_event_id, NULL AS updated_at,
                               NULL AS owner_email
                        FROM events AS e
                        JOIN conversations AS c ON c.id = e.conversation_id
                        WHERE (c.visibility = 'shared' OR c.owner_email = ?)
                    """
                    event_parameters = (actor.email,)
                rows.extend(connection.execute(event_sql, event_parameters).fetchall())

            results = [self._search_result(connection, row, actor) for row in rows]
        results.sort(key=lambda item: item.created_at, reverse=True)
        if normalized_query:
            results.sort(key=lambda item: item.rank)
        return results if bounded_limit is None else results[:bounded_limit]

    def update_memory(
        self,
        memory_id: int,
        *,
        importance: float | None = None,
        tags: Iterable[str] | None = None,
        actor: Actor = VOICE_ACTOR,
    ) -> bool:
        normalized_tags = None
        if tags is not None:
            normalized_tags = sorted(
                {tag.strip().casefold() for tag in tags if tag.strip()}
            )
        with self.connect() as connection:
            accessible = connection.execute(
                """
                SELECT 1 FROM memories
                WHERE id = ? AND (visibility = 'shared' OR owner_email = ?)
                """,
                (memory_id, actor.email),
            ).fetchone()
            if accessible is None:
                return False
            if importance is not None:
                connection.execute(
                    "UPDATE memories SET importance = ? WHERE id = ?",
                    (importance, memory_id),
                )
            if normalized_tags is not None:
                connection.execute(
                    "DELETE FROM memory_tags WHERE memory_id = ?", (memory_id,)
                )
                for tag_name in normalized_tags:
                    connection.execute(
                        "INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag_name,)
                    )
                    connection.execute(
                        """
                        INSERT INTO memory_tags(memory_id, tag_id)
                        SELECT ?, id FROM tags WHERE name = ?
                        """,
                        (memory_id, tag_name),
                    )
                connection.execute(
                    "DELETE FROM tags WHERE NOT EXISTS "
                    "(SELECT 1 FROM memory_tags WHERE memory_tags.tag_id = tags.id)"
                )
            if importance is not None or normalized_tags is not None:
                connection.execute(
                    "UPDATE memories SET updated_at = ? WHERE id = ?",
                    (utc_now(), memory_id),
                )
            return True

    def prune_preview(
        self,
        *,
        older_than_days: int | None = None,
        topic: str = "",
        actor: Actor = VOICE_ACTOR,
        limit: int = 500,
    ) -> tuple[list[SearchResult], DeletionResult]:
        if older_than_days is None and not topic.strip():
            raise ValueError("an age or topic is required")
        candidates = self.search(topic, limit=None, actor=actor)
        if older_than_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            candidates = [
                item
                for item in candidates
                if datetime.fromisoformat(item.created_at) < cutoff
            ]
        candidates = candidates[: max(1, min(limit, 5000))]
        selections = [(item.record_type, item.record_id) for item in candidates]
        with self.connect() as connection:
            _, derived, embeddings = self._deletion_plan(
                connection, selections, actor
            )
        selected_memories = {
            item.record_id for item in candidates if item.record_type == "memory"
        }
        return candidates, DeletionResult(
            len(candidates), len(derived - selected_memories), embeddings
        )

    def delete_records(
        self,
        records: Iterable[tuple[str, int]],
        *,
        actor: Actor = VOICE_ACTOR,
    ) -> DeletionResult:
        selections = list(dict.fromkeys(records))
        if not selections:
            return DeletionResult(0, 0, 0)
        with self.connect() as connection:
            plan, derived, embeddings = self._deletion_plan(
                connection, selections, actor
            )
            selected_memories = {
                identifier for kind, identifier in plan if kind == "memory"
            }
            selected_events = {
                identifier for kind, identifier in plan if kind == "event"
            }
            memory_ids = selected_memories | derived
            for memory_id in memory_ids:
                connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            for event_id in selected_events:
                connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            connection.execute(
                "DELETE FROM tags WHERE NOT EXISTS "
                "(SELECT 1 FROM memory_tags WHERE memory_tags.tag_id = tags.id)"
            )
        return DeletionResult(len(plan), len(derived - selected_memories), embeddings)

    def delete_memory(
        self, memory_id: int, *, actor: Actor = VOICE_ACTOR
    ) -> bool:
        try:
            result = self.delete_records((("memory", memory_id),), actor=actor)
        except PermissionError:
            return False
        return result.records_deleted == 1

    def delete_event(
        self,
        event_id: int,
        *,
        delete_derived: bool = True,
        actor: Actor = VOICE_ACTOR,
    ) -> bool:
        if not delete_derived:
            with self.connect() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM events
                    WHERE id IN (
                        SELECT e.id FROM events AS e
                        JOIN conversations AS c ON c.id = e.conversation_id
                        WHERE e.id = ?
                          AND (c.visibility = 'shared' OR c.owner_email = ?)
                    )
                    """,
                    (event_id, actor.email),
                )
                return cursor.rowcount == 1
        try:
            result = self.delete_records((("event", event_id),), actor=actor)
        except PermissionError:
            return False
        return result.records_deleted == 1

    def _search_result(
        self, connection: sqlite3.Connection, row: sqlite3.Row, actor: Actor
    ) -> SearchResult:
        if row["record_type"] == "memory":
            tags = tuple(
                value["name"]
                for value in connection.execute(
                    """
                    SELECT t.name FROM tags AS t
                    JOIN memory_tags AS mt ON mt.tag_id = t.id
                    WHERE mt.memory_id = ? ORDER BY t.name COLLATE NOCASE
                    """,
                    (row["id"],),
                )
            )
            sources: list[Mapping[str, object]] = [
                {
                    "source_type": value["source_type"],
                    "source_id": value["source_id"],
                    "uri": value["uri"],
                }
                for value in connection.execute(
                    """
                    SELECT source_type, source_id, uri FROM source_links
                    WHERE memory_id = ? ORDER BY id
                    """,
                    (row["id"],),
                )
            ]
            if row["source_event_id"] is not None:
                source = connection.execute(
                    """
                    SELECT e.id, e.conversation_id, e.kind, e.role, e.content,
                           e.created_at, e.actor_id, e.actor_source
                    FROM events AS e
                    JOIN conversations AS c ON c.id = e.conversation_id
                    WHERE e.id = ?
                      AND (c.visibility = 'shared' OR c.owner_email = ?)
                    """,
                    (row["source_event_id"], actor.email),
                ).fetchone()
                if source is not None:
                    sources.insert(
                        0,
                        {
                            "source_type": "transcript",
                            "source_id": str(source["id"]),
                            "conversation_id": source["conversation_id"],
                            "kind": source["kind"],
                            "role": source["role"],
                            "content": source["content"],
                            "created_at": source["created_at"],
                            "actor_id": source["actor_id"],
                            "actor_source": source["actor_source"],
                        },
                    )
            return SearchResult(
                record_type="memory",
                record_id=row["id"],
                content=row["content"],
                rank=float(row["rank"]),
                created_at=row["created_at"],
                kind=row["kind"],
                role=None,
                conversation_id=None,
                importance=float(row["importance"]),
                tags=tags,
                source_event_id=row["source_event_id"],
                sources=tuple(sources),
                visibility=row["visibility"],
                created_by=row["created_by"],
            )
        return SearchResult(
            record_type="event",
            record_id=row["id"],
            content=row["content"],
            rank=float(row["rank"]),
            created_at=row["created_at"],
            kind=row["kind"],
            role=row["role"],
            conversation_id=row["conversation_id"],
            importance=None,
            tags=(),
            source_event_id=None,
            sources=(
                {
                    "source_type": "transcript",
                    "source_id": str(row["id"]),
                    "conversation_id": row["conversation_id"],
                    "kind": row["kind"],
                    "role": row["role"],
                    "actor_id": row["actor_id"],
                    "actor_source": row["actor_source"],
                },
            ),
            visibility=row["visibility"],
            created_by=row["actor_id"],
        )

    def _deletion_plan(
        self,
        connection: sqlite3.Connection,
        selections: Iterable[tuple[str, int]],
        actor: Actor,
    ) -> tuple[set[tuple[str, int]], set[int], int]:
        plan = set(selections)
        if any(kind not in {"memory", "event"} for kind, _ in plan):
            raise ValueError("record type must be memory or event")
        selected_memories = {
            identifier for kind, identifier in plan if kind == "memory"
        }
        selected_events = {identifier for kind, identifier in plan if kind == "event"}
        for memory_id in selected_memories:
            accessible = connection.execute(
                """
                SELECT 1 FROM memories
                WHERE id = ? AND (visibility = 'shared' OR owner_email = ?)
                """,
                (memory_id, actor.email),
            ).fetchone()
            if accessible is None:
                raise PermissionError("memory record is not accessible")
        for event_id in selected_events:
            accessible = connection.execute(
                """
                SELECT 1 FROM events AS e
                JOIN conversations AS c ON c.id = e.conversation_id
                WHERE e.id = ? AND (c.visibility = 'shared' OR c.owner_email = ?)
                """,
                (event_id, actor.email),
            ).fetchone()
            if accessible is None:
                raise PermissionError("transcript record is not accessible")

        derived: set[int] = set()
        for event_id in selected_events:
            derived.update(
                value["id"]
                for value in connection.execute(
                    """
                    SELECT m.id FROM memories AS m
                    WHERE m.source_event_id = ? OR EXISTS (
                        SELECT 1 FROM source_links AS sl
                        WHERE sl.memory_id = m.id
                          AND sl.source_type IN ('event', 'transcript')
                          AND sl.source_id = ?
                    )
                    """,
                    (event_id, str(event_id)),
                )
            )
        frontier = set(selected_memories) | derived
        visited: set[int] = set()
        while frontier:
            source_id = frontier.pop()
            if source_id in visited:
                continue
            visited.add(source_id)
            dependents = {
                value["id"]
                for value in connection.execute(
                    """
                    SELECT m.id FROM memories AS m
                    JOIN source_links AS sl ON sl.memory_id = m.id
                    WHERE sl.source_type = 'memory' AND sl.source_id = ?
                    """,
                    (str(source_id),),
                )
            }
            derived.update(dependents)
            frontier.update(dependents - visited)
        all_memory_ids = selected_memories | derived
        embeddings = sum(
            connection.execute(
                "SELECT count(*) FROM memory_embeddings WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()[0]
            for memory_id in all_memory_ids
        )
        return plan, derived, embeddings

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
