from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = Path(__file__).parents[2] / "ops" / "miso_backup.py"
SPEC = importlib.util.spec_from_file_location("miso_backup", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
miso_backup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = miso_backup
SPEC.loader.exec_module(miso_backup)


class MisoBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "db" / "miso.sqlite3"
        self.database.parent.mkdir()
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE conversations(id TEXT PRIMARY KEY, content TEXT);
                CREATE TABLE events(id INTEGER PRIMARY KEY, content TEXT);
                CREATE TABLE memories(id INTEGER PRIMARY KEY, content TEXT);
                INSERT INTO conversations VALUES ('household', 'café mañana');
                INSERT INTO events(content) VALUES ('turn off the kitchen light');
                INSERT INTO memories(content) VALUES ('recycling on Friday');
                PRAGMA user_version = 3;
                """
            )
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "audit.jsonl").write_text('{"ok":true}\n', encoding="utf-8")
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "miso.env").write_text("SECRET=local-only\n", encoding="utf-8")
        self.key = self.root / "backup.key"
        self.key.write_text("unit-test-recovery-passphrase\n", encoding="utf-8")
        self.backups = self.root / "t7" / "backups" / "miso"
        self.backups.mkdir(parents=True)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.settings = miso_backup.Settings(
            backup_root=self.backups,
            key_file=self.key,
            t7_root=self.root / "t7",
            t7_uuid="test",
            database=self.database,
            state_dir=self.state,
            config_dir=self.config,
            staging_parent=self.staging,
            lock_file=self.root / "backup.lock",
            retention_points=30,
            allocation_max_bytes=10 * 1024 * 1024,
            root_warning_bytes=0,
            root_hard_min_bytes=0,
            t7_warning_bytes=0,
            t7_hard_min_bytes=0,
            skip_platform_checks=True,
            allow_non_root=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_backup_is_encrypted_atomic_and_passes_both_restore_checks(self) -> None:
        archive = miso_backup.backup(self.settings)

        self.assertTrue(archive.is_file())
        self.assertTrue(Path(f"{archive}.sha256").is_file())
        self.assertFalse(list(self.backups.glob("*.partial")))
        self.assertNotIn(b"local-only", archive.read_bytes())
        self.assertEqual(
            miso_backup.verify(self.settings, archive, full=False), archive
        )
        self.assertEqual(miso_backup.verify(self.settings, archive, full=True), archive)

    def test_corruption_is_detected_before_decryption(self) -> None:
        archive = miso_backup.backup(self.settings)
        content = bytearray(archive.read_bytes())
        content[len(content) // 2] ^= 0x01
        archive.write_bytes(content)

        with self.assertRaisesRegex(
            miso_backup.BackupError, "encrypted backup checksum failed"
        ):
            miso_backup.verify(self.settings, archive, full=True)

    def test_retention_keeps_newest_verified_points(self) -> None:
        settings = self.settings.__class__(
            **{**self.settings.__dict__, "retention_points": 2}
        )
        created = [miso_backup.backup(settings) for _ in range(3)]

        points = miso_backup.archive_points(self.backups)
        self.assertEqual([point[0] for point in points], created[-2:])
        self.assertFalse(created[0].exists())
        self.assertTrue(created[-1].exists())

    def test_oversize_new_backup_does_not_delete_previous_point(self) -> None:
        previous = miso_backup.backup(self.settings)
        settings = self.settings.__class__(
            **{**self.settings.__dict__, "allocation_max_bytes": 1}
        )

        with self.assertRaisesRegex(miso_backup.BackupError, "alone exceeds"):
            miso_backup.backup(settings)

        self.assertTrue(previous.exists())
        self.assertEqual(len(miso_backup.archive_points(self.backups)), 1)


if __name__ == "__main__":
    unittest.main()
