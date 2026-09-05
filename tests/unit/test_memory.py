from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from miso.identity import VOICE_ACTOR, web_actor
from miso.memory import MIGRATION_1, MIGRATION_2, MemoryStore, SCHEMA_VERSION


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "miso.sqlite3"
        self.store = MemoryStore(self.path)
        self.store.migrate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migrations_are_repeatable_and_enable_wal(self) -> None:
        self.store.migrate()
        with self.store.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertEqual(journal, "wal")
        self.assertEqual(self.store.integrity_check(), "ok")

    def test_household_settings_round_trip_and_clear(self) -> None:
        self.assertIsNone(self.store.read_setting("weather.location"))
        self.store.write_setting("weather.location", "London")
        self.assertEqual(self.store.read_setting("weather.location"), "London")
        self.store.write_setting("weather.location", "Madrid")
        self.assertEqual(self.store.read_setting("weather.location"), "Madrid")
        self.store.write_setting("weather.location", None)
        self.assertIsNone(self.store.read_setting("weather.location"))
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT actor_id FROM household_settings WHERE key = ?",
                ("weather.location",),
            ).fetchone()
        self.assertEqual(row["actor_id"], VOICE_ACTOR.actor_id)

    def test_v2_database_migrates_existing_records_to_shared_voice_identity(self) -> None:
        legacy_path = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            f"{MIGRATION_1}\n{MIGRATION_2}\nPRAGMA user_version = 2;"
        )
        connection.execute(
            "INSERT INTO conversations(id, created_at, updated_at) VALUES (?, ?, ?)",
            ("legacy", "2026-08-22T00:00:00+00:00", "2026-08-22T00:00:00+00:00"),
        )
        connection.commit()
        connection.close()

        legacy = MemoryStore(legacy_path)
        legacy.migrate()
        with legacy.connect() as migrated:
            row = migrated.execute(
                "SELECT visibility, owner_email, created_by FROM conversations"
            ).fetchone()
        self.assertEqual(tuple(row), ("shared", None, VOICE_ACTOR.actor_id))
        self.assertEqual(legacy.integrity_check(), "ok")

    def test_searches_english_and_spanish_events_and_memories(self) -> None:
        conversation = self.store.create_conversation("household")
        event = self.store.append_event(
            conversation,
            kind="message",
            role="user",
            content="Recuérdame comprar café mañana",
        )
        self.store.add_memory(
            "The blue recycling bin is collected on Friday",
            source_event_id=event,
            tags=("Household", "Recycling"),
            source_links=(
                {
                    "source_type": "conversation",
                    "source_id": conversation,
                    "uri": None,
                },
            ),
        )
        spanish = self.store.search("cafe")
        english = self.store.search("recycling")
        self.assertEqual(spanish[0].record_type, "event")
        self.assertIn("café", spanish[0].content)
        self.assertEqual(english[0].record_type, "memory")
        self.assertEqual(english[0].tags, ("household", "recycling"))
        self.assertEqual(english[0].sources[0]["source_type"], "transcript")
        self.assertIn("Recuérdame", english[0].sources[0]["content"])

    def test_browses_filters_and_updates_memory_controls(self) -> None:
        routine = self.store.add_memory(
            "Take the bins out every Thursday",
            kind="routine",
            importance=0.6,
            tags=("household", "bins"),
        )
        self.store.add_memory(
            "The kitchen gets busy at seven",
            kind="inferred",
            importance=0.9,
            tags=("important",),
        )

        recent = self.store.search("")
        routines = self.store.search("", kinds=("routine",))
        tagged = self.store.search("", tag="BINS")
        self.assertEqual(len(recent), 2)
        self.assertEqual([item.record_id for item in routines], [routine])
        self.assertEqual([item.record_id for item in tagged], [routine])

        self.assertTrue(
            self.store.update_memory(
                routine, importance=1.0, tags=("important", "recycling")
            )
        )
        updated = self.store.search("", kinds=("routine",))[0]
        self.assertEqual(updated.importance, 1.0)
        self.assertEqual(updated.tags, ("important", "recycling"))

    def test_prune_preview_and_delete_include_summaries_and_embeddings(self) -> None:
        conversation = self.store.create_conversation()
        event = self.store.append_event(
            conversation,
            kind="message",
            role="user",
            content="temporary travel plan",
        )
        base = self.store.add_memory(
            "temporary destination",
            source_event_id=event,
            tags=("temporary",),
        )
        summary = self.store.add_memory(
            "temporary trip summary",
            kind="summary",
            source_links=(
                {
                    "source_type": "memory",
                    "source_id": str(base),
                    "uri": None,
                },
            ),
        )
        with self.store.connect() as connection:
            for memory_id in (base, summary):
                connection.execute(
                    """
                    INSERT INTO memory_embeddings(memory_id, model, embedding, created_at)
                    VALUES (?, 'test-model', ?, '2026-08-24T00:00:00+00:00')
                    """,
                    (memory_id, b"vector"),
                )

        candidates, impact = self.store.prune_preview(topic="travel")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(impact.derived_memories_deleted, 2)
        self.assertEqual(impact.embeddings_deleted, 2)

        deleted = self.store.delete_records((("event", event),))
        self.assertEqual(deleted.records_deleted, 1)
        self.assertEqual(deleted.derived_memories_deleted, 2)
        self.assertEqual(deleted.embeddings_deleted, 2)
        self.assertFalse(self.store.search("temporary"))
        with self.store.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM memory_embeddings"
                ).fetchone()[0],
                0,
            )

    def test_delete_event_removes_derived_memory_and_fts_rows(self) -> None:
        conversation = self.store.create_conversation()
        event = self.store.append_event(
            conversation, kind="message", content="temporary source"
        )
        memory = self.store.add_memory(
            "derived temporary fact", source_event_id=event, tags=("temporary",)
        )
        self.assertTrue(self.store.search("derived"))
        self.assertTrue(self.store.delete_event(event))
        self.assertFalse(self.store.search("derived"))
        with self.store.connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM memories WHERE id = ?", (memory,)
                ).fetchone()
            )
            self.assertEqual(connection.execute("SELECT count(*) FROM tags").fetchone()[0], 0)

    def test_failed_write_rolls_back(self) -> None:
        conversation = self.store.create_conversation()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_memory("invalid kind", kind="unknown")
        with self.store.connect() as connection:
            count = connection.execute("SELECT count(*) FROM memories").fetchone()[0]
        self.assertEqual(count, 0)

    def test_lists_conversation_events_in_original_order(self) -> None:
        conversation = self.store.create_conversation()
        first = self.store.append_event(
            conversation, kind="message", role="user", content="first"
        )
        second = self.store.append_event(
            conversation,
            kind="tool",
            role="assistant",
            content="timer_create",
            payload={"ok": True},
        )
        self.assertTrue(self.store.conversation_exists(conversation))
        self.assertFalse(self.store.conversation_exists("missing"))
        events = self.store.events(conversation)
        self.assertEqual([event.event_id for event in events], [first, second])
        self.assertEqual(events[1].payload, {"ok": True})

    def test_private_records_are_visible_only_to_the_owning_web_member(self) -> None:
        juan = web_actor("juan@example.com")
        ana = web_actor("ana@example.com")
        self.store.provision_household_members((juan.email, ana.email))
        private = self.store.create_conversation(
            actor=juan, visibility="private"
        )
        event = self.store.append_event(
            private,
            kind="message",
            role="user",
            content="Juan's private calendar note",
            actor=juan,
        )
        self.store.add_memory(
            "Juan's private passport reminder",
            actor=juan,
            visibility="private",
        )

        self.assertTrue(self.store.conversation_exists(private, actor=juan))
        self.assertFalse(self.store.conversation_exists(private, actor=ana))
        self.assertFalse(self.store.conversation_exists(private, actor=VOICE_ACTOR))
        self.assertTrue(self.store.search("passport", actor=juan))
        self.assertFalse(self.store.search("passport", actor=ana))
        with self.assertRaises(PermissionError):
            self.store.events(private, actor=ana)
        self.assertFalse(self.store.delete_event(event, actor=ana))
        self.assertTrue(self.store.delete_event(event, actor=juan))

    def test_shared_voice_records_are_explicitly_attributed_and_web_visible(self) -> None:
        member = web_actor("member@example.com")
        self.store.provision_household_members((member.email,))
        conversation = self.store.create_conversation(actor=VOICE_ACTOR)
        self.store.append_event(
            conversation,
            kind="message",
            content="voice household note",
            actor=VOICE_ACTOR,
        )
        self.assertTrue(self.store.conversation_exists(conversation, actor=member))
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT created_by, visibility FROM conversations WHERE id = ?",
                (conversation,),
            ).fetchone()
            event = connection.execute(
                "SELECT actor_id, actor_source FROM events WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
        self.assertEqual(tuple(row), ("household:voice", "shared"))
        self.assertEqual(tuple(event), ("household:voice", "voice"))


if __name__ == "__main__":
    unittest.main()
