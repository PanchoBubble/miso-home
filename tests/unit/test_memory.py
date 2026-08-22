from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from miso.memory import MemoryStore, SCHEMA_VERSION


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


if __name__ == "__main__":
    unittest.main()
