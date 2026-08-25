#!/usr/bin/env python3
"""Create and independently restore-check encrypted Miso backup archives."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator


FORMAT_VERSION = 1
ARCHIVE_PREFIX = "miso-"
ARCHIVE_SUFFIX = ".tar.gz.enc"
KDF_ITERATIONS = 600_000
GIB = 1024**3


class BackupError(RuntimeError):
    """An expected backup or verification failure."""


def log(message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"{timestamp} {message}", flush=True)


def env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value < 0:
        raise BackupError(f"{name} cannot be negative")
    return value


@dataclass(frozen=True)
class Settings:
    backup_root: Path
    key_file: Path
    t7_root: Path
    t7_uuid: str
    database: Path
    state_dir: Path
    config_dir: Path
    staging_parent: Path
    lock_file: Path
    retention_points: int
    allocation_max_bytes: int
    root_warning_bytes: int
    root_hard_min_bytes: int
    t7_warning_bytes: int
    t7_hard_min_bytes: int
    skip_platform_checks: bool
    allow_non_root: bool

    @classmethod
    def from_environment(cls) -> "Settings":
        t7_root = Path(os.environ.get("MISO_BACKUP_T7_ROOT", "/media/pancho/T7"))
        return cls(
            backup_root=Path(
                os.environ.get(
                    "MISO_BACKUP_ROOT", str(t7_root / "backups" / "miso")
                )
            ),
            key_file=Path(
                os.environ.get(
                    "MISO_BACKUP_KEY_FILE",
                    "/home/pancho/.config/miso-backup/backup.key",
                )
            ),
            t7_root=t7_root,
            t7_uuid=os.environ.get("MISO_BACKUP_T7_UUID", "081E-DA7A"),
            database=Path(
                os.environ.get("MISO_BACKUP_DATABASE", "/var/lib/miso/db/miso.sqlite3")
            ),
            state_dir=Path(
                os.environ.get("MISO_BACKUP_STATE_DIR", "/var/lib/miso/state")
            ),
            config_dir=Path(os.environ.get("MISO_BACKUP_CONFIG_DIR", "/etc/miso")),
            staging_parent=Path(
                os.environ.get("MISO_BACKUP_STAGING_PARENT", "/var/lib/miso")
            ),
            lock_file=Path(
                os.environ.get(
                    "MISO_BACKUP_LOCK_FILE", "/run/lock/miso-database-backup.lock"
                )
            ),
            retention_points=env_int("MISO_BACKUP_RETENTION_POINTS", 30),
            allocation_max_bytes=env_int("MISO_BACKUP_ALLOCATION_MAX_GIB", 20) * GIB,
            root_warning_bytes=env_int("MISO_BACKUP_ROOT_WARNING_GIB", 30) * GIB,
            root_hard_min_bytes=env_int("MISO_BACKUP_ROOT_HARD_MIN_GIB", 20) * GIB,
            t7_warning_bytes=env_int("MISO_BACKUP_T7_WARNING_GIB", 200) * GIB,
            t7_hard_min_bytes=env_int("MISO_BACKUP_T7_HARD_MIN_GIB", 100) * GIB,
            skip_platform_checks=os.environ.get("MISO_BACKUP_SKIP_PLATFORM_CHECKS")
            == "1",
            allow_non_root=os.environ.get("MISO_BACKUP_ALLOW_NON_ROOT") == "1",
        )


def require_commands(*commands: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise BackupError(f"missing command(s): {', '.join(missing)}")


def run_checked(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(arguments, check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        raise BackupError(f"command failed: {arguments[0]}") from error


def available_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def format_gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def validate_space(settings: Settings) -> None:
    root_free = available_bytes(settings.staging_parent)
    t7_free = available_bytes(settings.backup_root)
    if root_free < settings.root_hard_min_bytes:
        raise BackupError(
            f"root free space {format_gib(root_free)} is below hard minimum "
            f"{format_gib(settings.root_hard_min_bytes)}"
        )
    if t7_free < settings.t7_hard_min_bytes:
        raise BackupError(
            f"T7 free space {format_gib(t7_free)} is below hard minimum "
            f"{format_gib(settings.t7_hard_min_bytes)}"
        )
    if root_free < settings.root_warning_bytes:
        log(f"WARNING: root free space is {format_gib(root_free)}")
    if t7_free < settings.t7_warning_bytes:
        log(f"WARNING: T7 free space is {format_gib(t7_free)}")


def mounted_filesystem(path: Path) -> tuple[str, str]:
    result = run_checked(
        ["findmnt", "-rn", "-o", "FSTYPE,UUID", "-T", str(path)],
        capture_output=True,
        text=True,
    )
    candidates = [
        fields
        for line in result.stdout.splitlines()
        if len(fields := line.split()) >= 2
    ]
    if not candidates:
        raise BackupError(f"could not identify mounted filesystem for {path}")
    fields = candidates[-1]
    return fields[0], fields[1]


def validate_environment(settings: Settings, *, require_sources: bool) -> None:
    if os.geteuid() != 0 and not settings.allow_non_root:
        raise BackupError("run as root")
    if settings.retention_points < 1:
        raise BackupError("at least one verified recovery point must be retained")
    if settings.allocation_max_bytes < 1:
        raise BackupError("backup allocation must be greater than zero")
    require_commands("openssl")
    if require_sources:
        if not settings.database.is_file():
            raise BackupError(f"database not found: {settings.database}")
        if not settings.state_dir.is_dir():
            raise BackupError(f"state directory not found: {settings.state_dir}")
        if not settings.config_dir.is_dir():
            raise BackupError(f"configuration directory not found: {settings.config_dir}")
    if not settings.key_file.is_file() or not os.access(settings.key_file, os.R_OK):
        raise BackupError(f"backup key is not readable: {settings.key_file}")
    if settings.key_file.stat().st_size == 0:
        raise BackupError(f"backup key is empty: {settings.key_file}")
    settings.staging_parent.mkdir(parents=True, exist_ok=True)
    settings.backup_root.mkdir(parents=True, exist_ok=True)
    settings.lock_file.parent.mkdir(parents=True, exist_ok=True)
    if not settings.skip_platform_checks:
        require_commands("findmnt")
        try:
            settings.backup_root.resolve().relative_to(settings.t7_root.resolve())
        except ValueError as error:
            raise BackupError(
                "backup root must remain beneath the verified T7 mount"
            ) from error
        if settings.key_file.stat().st_mode & 0o077:
            raise BackupError(
                "backup key must not be accessible by group or others: "
                f"{settings.key_file}"
            )
        state_fstype, _ = mounted_filesystem(settings.staging_parent)
        if state_fstype != "ext4":
            raise BackupError(
                f"plaintext staging must be on ext4, found {state_fstype}"
            )
        _, mounted_uuid = mounted_filesystem(settings.t7_root)
        if mounted_uuid != settings.t7_uuid:
            raise BackupError(
                f"T7 UUID {settings.t7_uuid} is not mounted at {settings.t7_root}"
            )
    validate_space(settings)


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BackupError("another Miso backup or restore check is running") from error
        yield


def sqlite_check(database: Path, *, full: bool) -> dict[str, int | str]:
    try:
        # A backed-up WAL database may need to create transient -shm/-wal files
        # when first opened, so validate it in the private writable staging area.
        with sqlite3.connect(database) as connection:
            check = "integrity_check" if full else "quick_check"
            result = connection.execute(f"PRAGMA {check}").fetchone()[0]
            if result != "ok":
                raise BackupError(f"SQLite {check} failed: {result}")
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise BackupError("SQLite foreign key check failed")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = int(
                connection.execute(
                    "SELECT count(*) FROM sqlite_schema "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            )
        return {"user_version": version, "table_count": tables, "check": check}
    except sqlite3.Error as error:
        raise BackupError(f"cannot validate SQLite database: {error}") from error


def create_sqlite_snapshot(source: Path, destination: Path) -> dict[str, int | str]:
    try:
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as live:
            with sqlite3.connect(destination) as snapshot:
                live.backup(snapshot)
        # The backup API copies the source database's WAL-mode header. Opening
        # that snapshot for validation can therefore create regenerated -wal
        # and -shm files beside it. They are not part of the recovery point and
        # can change while checksums and the tar stream are being produced.
        # Switch the private snapshot to rollback-journal mode so all committed
        # pages remain in the main file, then discard only those WAL sidecars.
        with sqlite3.connect(destination) as snapshot:
            journal_mode = snapshot.execute(
                "PRAGMA journal_mode = DELETE"
            ).fetchone()[0]
        if str(journal_mode).lower() != "delete":
            raise BackupError(
                f"SQLite snapshot journal normalization failed: {journal_mode}"
            )
        for suffix in ("-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
    except sqlite3.Error as error:
        raise BackupError(f"SQLite online backup failed: {error}") from error
    fsync_file(destination)
    metadata = sqlite_check(destination, full=True)
    for suffix in ("-wal", "-shm"):
        if Path(f"{destination}{suffix}").exists():
            raise BackupError(f"SQLite snapshot retained a transient {suffix} sidecar")
    return metadata


def copy_tree(source: Path, destination: Path) -> None:
    def regular_files_only(path: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        base = Path(path)
        for name in names:
            candidate = base / name
            if candidate.is_symlink() or not (candidate.is_dir() or candidate.is_file()):
                ignored.add(name)
        return ignored

    shutil.copytree(source, destination, symlinks=False, ignore=regular_files_only)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_checksums(payload: Path) -> dict[str, str]:
    return {
        path.relative_to(payload).as_posix(): sha256_file(path)
        for path in sorted(payload.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fsync_file(path)


def create_bundle(settings: Settings, stage: Path, created_utc: str) -> Path:
    bundle = stage / "bundle"
    payload = bundle / "payload"
    database_dir = payload / "database"
    database_dir.mkdir(parents=True)
    snapshot = database_dir / "miso.sqlite3"
    log("creating online SQLite snapshot")
    database_metadata = create_sqlite_snapshot(settings.database, snapshot)
    copy_tree(settings.state_dir, payload / "state")
    copy_tree(settings.config_dir, payload / "config")
    checksums = payload_checksums(payload)
    write_json(bundle / "checksums.json", checksums)
    write_json(
        bundle / "manifest.json",
        {
            "created_utc": created_utc,
            "database": database_metadata,
            "format_version": FORMAT_VERSION,
            "hostname": socket.gethostname(),
            "source_database": str(settings.database),
            "source_state_dir": str(settings.state_dir),
        },
    )
    plaintext = stage / "miso-backup.tar.gz"
    with tarfile.open(plaintext, "w:gz", compresslevel=6) as archive:
        archive.add(bundle, arcname=".", recursive=True)
    fsync_file(plaintext)
    return plaintext


def openssl(arguments: list[str]) -> None:
    run_checked(
        [
            "openssl",
            "enc",
            *arguments,
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            str(KDF_ITERATIONS),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def encrypt(plaintext: Path, encrypted: Path, key_file: Path) -> None:
    openssl(
        [
            "-e",
            "-salt",
            "-pass",
            f"file:{key_file}",
            "-in",
            str(plaintext),
            "-out",
            str(encrypted),
        ]
    )
    fsync_file(encrypted)


def decrypt(encrypted: Path, plaintext: Path, key_file: Path) -> None:
    openssl(
        [
            "-d",
            "-pass",
            f"file:{key_file}",
            "-in",
            str(encrypted),
            "-out",
            str(plaintext),
        ]
    )
    fsync_file(plaintext)


def safe_extract(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            member_path = PurePosixPath(member.name)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or member.issym()
                or member.islnk()
                or not (member.isdir() or member.isfile())
            ):
                raise BackupError(f"unsafe archive member: {member.name}")
        try:
            archive.extractall(destination, filter="fully_trusted")
        except TypeError:  # Python versions before extraction filters were added.
            archive.extractall(destination)


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError(f"invalid backup metadata: {path.name}") from error


def validate_extracted_bundle(extracted: Path, *, full: bool) -> dict[str, object]:
    manifest_path = extracted / "manifest.json"
    checksums_path = extracted / "checksums.json"
    database = extracted / "payload" / "database" / "miso.sqlite3"
    if (
        not manifest_path.is_file()
        or not checksums_path.is_file()
        or not database.is_file()
    ):
        raise BackupError("backup is missing required members")
    manifest = load_json(manifest_path)
    checksums = load_json(checksums_path)
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("unsupported backup format")
    if not isinstance(checksums, dict) or not checksums:
        raise BackupError("backup checksums are missing")
    payload = extracted / "payload"
    for relative, expected in checksums.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise BackupError("backup checksum metadata is malformed")
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise BackupError("backup checksum path is unsafe")
        path = payload / relative
        if not path.is_file() or path.is_symlink() or sha256_file(path) != expected:
            raise BackupError(f"backup member checksum failed: {relative}")
    if payload_checksums(payload) != checksums:
        raise BackupError("backup payload does not exactly match its checksum manifest")
    metadata = sqlite_check(database, full=full)
    if full:
        restored = extracted / "isolated-restore.sqlite3"
        create_sqlite_snapshot(database, restored)
        restored_metadata = sqlite_check(restored, full=True)
        if restored_metadata["user_version"] != metadata["user_version"]:
            raise BackupError("isolated restore changed the database schema version")
        with sqlite3.connect(restored) as connection:
            required = {"conversations", "events", "memories"}
            present = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table'"
                )
            }
        missing = sorted(required - present)
        if missing:
            raise BackupError(
                f"isolated restore is missing application tables: {', '.join(missing)}"
            )
    return manifest


def verify_outer_checksum(archive: Path) -> None:
    checksum_path = Path(f"{archive}.sha256")
    if not checksum_path.is_file():
        raise BackupError(f"backup checksum not found: {checksum_path}")
    fields = checksum_path.read_text(encoding="ascii").strip().split()
    if len(fields) != 2 or fields[1].lstrip("*") != archive.name:
        raise BackupError("backup checksum sidecar is malformed")
    if fields[0] != sha256_file(archive):
        raise BackupError("encrypted backup checksum failed")


def verify_archive(
    archive: Path,
    settings: Settings,
    *,
    full: bool,
    require_outer_checksum: bool,
) -> dict[str, object]:
    if not archive.is_file():
        raise BackupError(f"backup archive not found: {archive}")
    if require_outer_checksum:
        verify_outer_checksum(archive)
    with tempfile.TemporaryDirectory(
        prefix=".miso-restore-check.", dir=settings.staging_parent
    ) as temporary:
        stage = Path(temporary)
        plaintext = stage / "backup.tar.gz"
        extracted = stage / "extracted"
        extracted.mkdir()
        decrypt(archive, plaintext, settings.key_file)
        safe_extract(plaintext, extracted)
        return validate_extracted_bundle(extracted, full=full)


def fsync_file(path: Path) -> None:
    with path.open("rb") as item:
        os.fsync(item.fileno())


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some exFAT/kernel combinations do not expose directory fsync.
        log(f"WARNING: directory fsync is unavailable for {path}")


def archive_points(backup_root: Path) -> list[tuple[Path, Path, int]]:
    points: list[tuple[Path, Path, int]] = []
    for archive in sorted(backup_root.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}")):
        checksum = Path(f"{archive}.sha256")
        if checksum.is_file():
            points.append(
                (archive, checksum, archive.stat().st_size + checksum.stat().st_size)
            )
    return points


def rotate_verified_points(settings: Settings, newest: Path) -> None:
    points = archive_points(settings.backup_root)
    if not points or points[-1][0] != newest:
        raise BackupError("newest verified recovery point is not selectable")
    total = sum(point[2] for point in points)
    while (
        len(points) > settings.retention_points
        or total > settings.allocation_max_bytes
    ):
        if len(points) == 1:
            raise BackupError("newest recovery point alone exceeds backup allocation")
        archive, checksum, size = points.pop(0)
        log(f"removing expired recovery point: {archive.name}")
        archive.unlink()
        checksum.unlink(missing_ok=True)
        total -= size
    if total > settings.allocation_max_bytes:
        raise BackupError("backup allocation remains above its hard limit")
    log(
        f"retention complete: {len(points)} point(s), "
        f"{format_gib(total)} of {format_gib(settings.allocation_max_bytes)}"
    )


def unique_archive_name() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return f"{ARCHIVE_PREFIX}{now:%Y%m%dT%H%M%S}-{now.microsecond:06d}Z{ARCHIVE_SUFFIX}"


def write_checksum_sidecar(source: Path, published_archive: Path) -> Path:
    checksum = Path(f"{published_archive}.sha256")
    partial = Path(f"{checksum}.partial")
    try:
        partial.write_text(
            f"{sha256_file(source)}  {published_archive.name}\n", encoding="ascii"
        )
        fsync_file(partial)
        os.replace(partial, checksum)
        return checksum
    finally:
        partial.unlink(missing_ok=True)


def backup(settings: Settings) -> Path:
    validate_environment(settings, require_sources=True)
    with exclusive_lock(settings.lock_file):
        name = unique_archive_name()
        final = settings.backup_root / name
        partial = settings.backup_root / f".{name}.partial"
        checksum = Path(f"{final}.sha256")
        if final.exists() or partial.exists() or checksum.exists():
            raise BackupError(f"backup filename collision: {name}")
        published = False
        created_utc = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="microseconds"
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix=".miso-backup.", dir=settings.staging_parent
            ) as temporary:
                plaintext = create_bundle(settings, Path(temporary), created_utc)
                log("encrypting backup to T7 partial artifact")
                encrypt(plaintext, partial, settings.key_file)
                if partial.stat().st_size > settings.allocation_max_bytes:
                    raise BackupError(
                        "new backup alone exceeds the Miso backup allocation"
                    )
                log("performing isolated restore verification before publication")
                verify_archive(
                    partial,
                    settings,
                    full=True,
                    require_outer_checksum=False,
                )
                # Publish the checksum name first, then atomically expose its
                # already-verified archive. A crash cannot expose an archive
                # that the selector would mistake for a complete point.
                write_checksum_sidecar(partial, final)
                fsync_directory(settings.backup_root)
                os.replace(partial, final)
                published = True
                fsync_directory(settings.backup_root)
            rotate_verified_points(settings, final)
            log(f"backup complete: {final}")
            return final
        finally:
            partial.unlink(missing_ok=True)
            if not published:
                checksum.unlink(missing_ok=True)


def latest_archive(settings: Settings) -> Path:
    points = archive_points(settings.backup_root)
    if not points:
        raise BackupError(f"no verified backup found under {settings.backup_root}")
    return points[-1][0]


def verify(settings: Settings, archive: Path | None, *, full: bool) -> Path:
    validate_environment(settings, require_sources=False)
    with exclusive_lock(settings.lock_file):
        selected = archive or latest_archive(settings)
        log(f"starting {'full' if full else 'quick'} restore check: {selected}")
        manifest = verify_archive(
            selected,
            settings,
            full=full,
            require_outer_checksum=True,
        )
        log(
            "restore check passed: "
            f"{selected} (created {manifest.get('created_utc', 'unknown')})"
        )
        return selected


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backup", help="create, verify, and rotate a backup")
    verify_parser = subparsers.add_parser(
        "verify", help="decrypt and restore-check a backup in isolated staging"
    )
    mode = verify_parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="run quick SQLite checks")
    mode.add_argument("--full", action="store_true", help="exercise an isolated restore")
    verify_parser.add_argument("archive", nargs="?", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv or sys.argv[1:])
        settings = Settings.from_environment()
        if arguments.command == "backup":
            backup(settings)
        else:
            verify(settings, arguments.archive, full=arguments.full)
        return 0
    except BackupError as error:
        log(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
