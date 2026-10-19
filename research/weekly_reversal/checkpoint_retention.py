"""Opt-in append-only checkpoint archive. No execution or cleanup authority.

Keep all generations as hash-addressed fixed-size chunks plus exact original
manifests. Exhausting a pinned capacity plan stops admission, never evicts data.
There is deliberately no CLI, pruning API, runner hook, or historical restore.
See CHECKPOINT_RETENTION_CONTRACT.md for limits and the storage threat model.
"""

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

FILES = (
    "account/paper.sqlite",
    "account/native.sqlite",
    "account/allocator.sqlite",
    "caller.json",
    "events.jsonl",
    "marks.jsonl",
    "signals.jsonl",
)
SIDECARS = tuple(
    name + suffix for name in FILES[:3] for suffix in ("-wal", "-shm", "-journal")
)
MEMBERS = {"manifest.json", *FILES, *SIDECARS}
PLAN_KEYS = {
    "schema",
    "state",
    "workspace_root",
    "archive_root",
    "run_directory",
    "chunk_bytes",
    "max_archive_bytes",
    "reserve_bytes",
    "max_checkpoint_bytes",
    "max_generation_bytes",
    "max_generations",
}
GENERATION_KEYS = {
    "schema",
    "state",
    "sequence",
    "parent_sha256",
    "plan_sha256",
    "checkpoint_manifest_sha256",
    "at",
    "steps",
    "files",
}
# Charge each inode at least 64 KiB, including empty files and directories. This
# exceeds common filesystem per-file and directory-entry allocation overhead.
INODE_CHARGE = 65536


class RetentionError(ValueError):
    """A failed gate: preserve current evidence and do not continue implicitly."""


def _fail(message):
    raise RetentionError(message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        _fail("Exact lowercase SHA-256 required")
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail("Duplicate JSON field")
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda value: _fail("Nonfinite JSON"),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RetentionError("Invalid UTF-8 JSON") from exc


def _encode(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _canonical(value):
    if not isinstance(value, (str, Path)):
        _fail("Canonical absolute path required")
    text = str(value)
    path = Path(text)
    if not path.is_absolute() or str(path) != text or str(path.resolve()) != text:
        _fail("Canonical absolute path without symlink aliases required")
    return path


def _within(path, root):
    return root in path.parents


def _directory(path):
    """Open each absolute component without following symlinks."""
    path = _canonical(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _member(root_fd, name):
    if name not in MEMBERS:
        _fail("Unknown or unsafe checkpoint member")
    fd = os.dup(root_fd)
    try:
        parts = name.split("/")
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        leaf = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        try:
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail("Checkpoint members must be ordinary single-link files")
            with os.fdopen(leaf, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(leaf)
    finally:
        os.close(fd)


def _read_path(path, limit):
    path = _canonical(path)
    parent = _directory(path.parent)
    try:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > limit
            ):
                _fail("Oversized or nonordinary pinned file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit:
                _fail("Pinned file grew while reading")
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _rename_noreplace(source_fd, source, destination_fd, destination):
    """Linux atomic publication with no replacement, including directories."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        _fail("Atomic no-replace publication requires Linux renameat2")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if (
        rename(
            source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1
        )
        != 0
    ):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            _fail("Immutable publication destination already exists")
        raise OSError(code, os.strerror(code))


def _charge(info):
    return max(INODE_CHARGE, info.st_size, info.st_blocks * 512)


def _planned_charge(size):
    return INODE_CHARGE + ((size + 4095) // 4096) * 4096


def _plan(raw):
    plan = _json(raw)
    if (
        not isinstance(plan, dict)
        or set(plan) != PLAN_KEYS
        or type(plan["schema"]) is not int
        or plan["schema"] != 1
        or plan["state"]
        not in {"SYNTHETIC_ONLY_APPEND_ONLY", "ARCHIVE_ONLY_NO_EXECUTION"}
    ):
        _fail("Explicit exact retention plan required")
    for key in PLAN_KEYS - {
        "schema",
        "state",
        "workspace_root",
        "archive_root",
        "run_directory",
    }:
        if type(plan[key]) is not int or plan[key] < 1:
            _fail("Retention limits must be positive exact integers")
    root, archive, run = (
        _canonical(plan[key])
        for key in ("workspace_root", "archive_root", "run_directory")
    )
    if not root.is_dir() or not _within(archive, root) or not _within(run, root):
        _fail("Archive and run must be strictly inside existing workspace")
    if archive == run or _within(archive, run) or _within(run, archive):
        _fail("Archive and run directories must be disjoint")
    chunk = plan["chunk_bytes"]
    if chunk < 4096 or chunk > 4194304 or chunk & (chunk - 1):
        _fail("Chunk size must be a power of two from 4 KiB to 4 MiB")
    if (
        plan["max_generation_bytes"] > 16 * 1024 * 1024
        or plan["max_generations"] > 100000
        or plan["reserve_bytes"] < plan["max_checkpoint_bytes"] + 32 * INODE_CHARGE
        or plan["max_archive_bytes"] < 16 * INODE_CHARGE
    ):
        _fail("Plan lacks bounded metadata or a full checkpoint restore reserve")
    return plan


def _inventory(root_fd):
    names = set()
    for entry in os.scandir(root_fd):
        if entry.name == "account" and entry.is_dir(follow_symlinks=False):
            fd = os.open(
                "account", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
            )
            try:
                for child in os.scandir(fd):
                    if not child.is_file(follow_symlinks=False):
                        _fail("Checkpoint contains nonordinary account member")
                    names.add("account/" + child.name)
            finally:
                os.close(fd)
        elif entry.is_file(follow_symlinks=False):
            names.add(entry.name)
        else:
            _fail("Checkpoint contains nonordinary member")
    if not {"manifest.json", *FILES} <= names or not names <= MEMBERS:
        _fail("Checkpoint has missing or unrecognized evidence members")
    return names


def _describe(stream, chunk_size, limit):
    size, digest, chunks = 0, hashlib.sha256(), []
    while raw := stream.read(chunk_size):
        size += len(raw)
        if size > limit:
            _fail("Checkpoint member exceeded size limit while reading")
        digest.update(raw)
        chunks.append({"sha256": _sha(raw), "bytes": len(raw)})
    return {"bytes": size, "sha256": digest.hexdigest(), "chunks": chunks}


def _checkpoint(root_fd, plan, expected, *, verify_sources=True):
    """Validate immutable base databases without creating SQLite WAL/SHM files."""
    names = _inventory(root_fd)
    sizes = []
    for name in names:
        with _member(root_fd, name) as stream:
            sizes.append(os.fstat(stream.fileno()).st_size)
    if sum(sizes) > plan["max_checkpoint_bytes"]:
        _fail("Checkpoint exceeds size limit")
    with _member(root_fd, "manifest.json") as stream:
        raw = stream.read(plan["max_generation_bytes"] + 1)
    if len(raw) > plan["max_generation_bytes"] or _sha(raw) != _digest(expected):
        _fail("Checkpoint manifest identity or size differs")
    manifest = _json(raw)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema")) is not int
        or manifest["schema"] != 2
        or manifest.get("state") != "COMPLETED_BOUNDARY"
        or type(manifest.get("synthetic")) is not bool
        or (
            plan["state"] == "SYNTHETIC_ONLY_APPEND_ONLY"
            and manifest["synthetic"] is not True
        )
        or manifest.get("run_directory") != plan["run_directory"]
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != set(FILES)
        or not isinstance(manifest.get("input_identity"), dict)
        or manifest["input_identity"].get("synthetic") is not manifest["synthetic"]
        or any(
            type(manifest.get(key)) is not int or manifest[key] < 1
            for key in ("at", "steps")
        )
    ):
        _fail("Only exact completed checkpoints permitted by the plan are accepted")
    pins = manifest.get("source_pins")
    if not isinstance(pins, dict) or not pins:
        _fail("Nonempty source pins required")
    for path, expected_pin in pins.items():
        if (
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or str(Path(path)) != path
            or ".." in Path(path).parts
        ):
            _fail("Source pin must retain a canonical absolute path")
        _digest(expected_pin)
        if verify_sources and _sha(_read_path(path, 32 * 1024 * 1024)) != expected_pin:
            _fail("Checkpoint source identity changed")
    records = {}
    for name in sorted(names):
        with _member(root_fd, name) as stream:
            records[name] = _describe(
                stream, plan["chunk_bytes"], plan["max_checkpoint_bytes"]
            )
        record = records[name]
        if name in FILES:
            original = manifest["files"][name]
            if (
                not isinstance(original, dict)
                or set(original) != {"bytes", "sha256"}
                or type(original["bytes"]) is not int
                or original["bytes"] < 0
                or original != {key: record[key] for key in ("bytes", "sha256")}
            ):
                _fail("Checkpoint file identity differs: " + name)
        if name.endswith(("-wal", "-journal")) and record["bytes"]:
            _fail("Unpinned nonempty SQLite WAL/journal evidence cannot be admitted")
    if records["manifest.json"]["sha256"] != expected:
        _fail("Checkpoint manifest changed while its members were described")
    if (
        sum(record["bytes"] for record in records.values())
        > plan["max_checkpoint_bytes"]
    ):
        _fail("Checkpoint grew beyond size limit")
    with _member(root_fd, "caller.json") as stream:
        caller = _json(stream.read(plan["max_checkpoint_bytes"] + 1))
    for key in ("steps", "protocol_sha256", "input_identity"):
        if _encode(caller.get(key)) != _encode(manifest[key]):
            _fail("Caller identity differs from completed checkpoint")
    if type(caller.get("last_at")) is not int or caller["last_at"] != manifest["at"]:
        _fail("Caller cursor differs from completed checkpoint")
    for name in FILES[:3]:
        with _member(root_fd, name) as stream:
            # Linux /proc duplicates a verified, no-follow descriptor. immutable
            # suppresses all journal discovery and sidecar creation.
            uri = (
                "file:"
                + quote("/proc/self/fd/" + str(stream.fileno()))
                + "?mode=ro&immutable=1"
            )
            try:
                with sqlite3.connect(uri, uri=True) as db:
                    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        _fail("Checkpoint SQLite integrity failure")
                    if name == FILES[0]:
                        state = _json(
                            db.execute("SELECT value FROM state WHERE id=1").fetchone()[
                                0
                            ]
                        )
                        if (
                            type(state.get("now")) not in (int, float)
                            or state["now"] != manifest["at"]
                        ):
                            _fail("Paper clock differs from completed checkpoint")
            except (sqlite3.Error, TypeError) as exc:
                raise RetentionError("Checkpoint SQLite integrity failure") from exc
    return manifest, records


class CheckpointArchive:
    """Exclusive context manager; all actions require enabled=True and a plan hash."""

    def __init__(
        self,
        plan_path,
        *,
        expected_plan_sha256,
        enabled=False,
        allow_non_synthetic_archival=False,
    ):
        if enabled is not True:
            _fail("Checkpoint retention is disabled; explicit opt-in required")
        self.plan_path = _canonical(plan_path)
        self.plan_raw = _read_path(self.plan_path, 65536)
        self.plan_sha256 = _digest(expected_plan_sha256)
        if _sha(self.plan_raw) != self.plan_sha256:
            _fail("Retention plan identity differs")
        self.plan = _plan(self.plan_raw)
        if (
            self.plan["state"] == "ARCHIVE_ONLY_NO_EXECUTION"
            and allow_non_synthetic_archival is not True
        ):
            _fail(
                "Archive-only non-synthetic preservation requires its separate explicit opt-in"
            )
        if (
            self.plan["state"] == "SYNTHETIC_ONLY_APPEND_ONLY"
            and allow_non_synthetic_archival is not False
        ):
            _fail(
                "Non-synthetic archival opt-in does not match the pinned synthetic-only plan"
            )
        self.root = Path(self.plan["archive_root"])
        self.root_fd = self.lock_fd = None
        self.pid = None
        self.directory_identities = {}

    def __enter__(self):
        if self.root_fd is not None:
            _fail("Archive ownership is already active")
        parent_fd = _directory(self.root.parent)
        try:
            try:
                os.mkdir(self.root.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
        finally:
            os.close(parent_fd)
        try:
            self.root_fd = _directory(self.root)
            info = os.fstat(self.root_fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                _fail("Archive root must be privately owned with mode 0700")
            self.root_identity = (info.st_dev, info.st_ino)
            self.lock_fd = os.open(
                "writer.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=self.root_fd,
            )
            info = os.fstat(self.lock_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
            ):
                _fail("Unsafe archive writer lock")
            self.lock_identity = (info.st_dev, info.st_ino)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RetentionError("Archive writer is already held") from exc
            self.pid = os.getpid()
            if not os.path.lexists(self.root / "plan.json"):
                if set(os.listdir(self.root_fd)) != {"writer.lock"}:
                    _fail("Incomplete archive initialization must be preserved")
                self._capacity(8 * INODE_CHARGE)
                for name in ("chunks", "generations", "staging", "restores"):
                    os.mkdir(name, 0o700, dir_fd=self.root_fd)
                self._publish("plan.json", self.plan_raw)
            for name in ("chunks", "generations", "staging", "restores"):
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=self.root_fd,
                )
                try:
                    info = os.fstat(child)
                    if (
                        info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700
                    ):
                        _fail("Archive child directories must be private")
                    self.directory_identities[name] = (info.st_dev, info.st_ino)
                finally:
                    os.close(child)
            self._owned()
            self.audit()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        for name in ("lock_fd", "root_fd"):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
        self.pid = None

    def _owned(self):
        if self.root_fd is None or self.pid != os.getpid():
            _fail("Current process does not own archive writer")
        root_fd = _directory(self.root)
        try:
            info = os.fstat(root_fd)
            if (info.st_dev, info.st_ino) != self.root_identity:
                _fail("Archive directory ownership changed")
        finally:
            os.close(root_fd)
        info = os.stat("writer.lock", dir_fd=self.root_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != self.lock_identity
            or info.st_nlink != 1
        ):
            _fail("Archive writer lock identity changed")
        for name, identity in self.directory_identities.items():
            info = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
                _fail("Archive child directory identity changed")
        if _read_path(self.plan_path, 65536) != self.plan_raw:
            _fail("Retention plan changed while owned")
        if _read_path(self.root / "plan.json", 65536) != self.plan_raw:
            _fail("Stored retention plan identity differs")

    def _usage(self):
        total = 0
        seen = set()

        def walk(fd):
            nonlocal total
            info = os.fstat(fd)
            total += _charge(info)
            for entry in os.scandir(fd):
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    try:
                        walk(child)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    identity = (info.st_dev, info.st_ino)
                    if identity not in seen:
                        seen.add(identity)
                        total += _charge(info)
                else:
                    _fail("Unsafe archive member")

        walk(self.root_fd)
        return total

    def _capacity(self, extra, *, restoring=False):
        used = self._usage()
        ceiling = self.plan["max_archive_bytes"] + (
            self.plan["reserve_bytes"] if restoring else 0
        )
        if used + extra > ceiling:
            _fail("Retention disk budget exhausted; preserve evidence and stop")
        fs = os.fstatvfs(self.root_fd)
        available = fs.f_bavail * fs.f_frsize
        reserve = 0 if restoring else self.plan["reserve_bytes"]
        if available < extra + reserve:
            _fail("Filesystem free-space reserve would be crossed")
        return used

    def _publish(self, relative, raw):
        # Caller holds the root lock. No replacing rename is ever used.
        if relative != "plan.json":
            self._owned()
        parent, name = relative.rsplit("/", 1) if "/" in relative else (None, relative)
        fd = (
            os.dup(self.root_fd)
            if parent is None
            else os.open(
                parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
        )
        temporary = ".partial-" + uuid.uuid4().hex
        try:
            out = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
                dir_fd=fd,
            )
            try:
                with os.fdopen(out, "wb", closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(out)
            finally:
                os.close(out)
            _rename_noreplace(fd, temporary, fd, name)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _blob(self, digest, expected_size=None):
        raw = _read_path(
            self.root / "chunks" / _digest(digest), self.plan["chunk_bytes"]
        )
        if _sha(raw) != digest or (
            expected_size is not None and len(raw) != expected_size
        ):
            _fail("Content-addressed chunk integrity differs")
        return raw

    def _record(self, record):
        if (
            not isinstance(record, dict)
            or set(record) != {"bytes", "sha256", "chunks"}
            or type(record["bytes"]) is not int
            or record["bytes"] < 0
            or record["bytes"] > self.plan["max_checkpoint_bytes"]
            or not isinstance(record["chunks"], list)
        ):
            _fail("Invalid checkpoint member descriptor")
        _digest(record["sha256"])
        total = 0
        for index, chunk in enumerate(record["chunks"]):
            if (
                not isinstance(chunk, dict)
                or set(chunk) != {"bytes", "sha256"}
                or type(chunk["bytes"]) is not int
                or not 0 < chunk["bytes"] <= self.plan["chunk_bytes"]
                or (
                    index < len(record["chunks"]) - 1
                    and chunk["bytes"] != self.plan["chunk_bytes"]
                )
            ):
                _fail("Invalid fixed-size chunk descriptor")
            _digest(chunk["sha256"])
            total += chunk["bytes"]
        if total != record["bytes"]:
            _fail("Chunk lengths differ from member size")

    def _generation(self, raw, sequence, parent):
        gen = _json(raw)
        if (
            not isinstance(gen, dict)
            or set(gen) != GENERATION_KEYS
            or type(gen["schema"]) is not int
            or gen["schema"] != 1
            or gen["state"] != "ARCHIVED_CHECKPOINT_NO_EXECUTION"
            or type(gen["sequence"]) is not int
            or gen["sequence"] != sequence
            or gen["parent_sha256"] != parent
            or gen["plan_sha256"] != self.plan_sha256
            or any(type(gen[key]) is not int or gen[key] < 1 for key in ("at", "steps"))
            or not isinstance(gen["files"], dict)
            or not {"manifest.json", *FILES} <= set(gen["files"])
            or not set(gen["files"]) <= MEMBERS
        ):
            _fail("Generation identity, chain, or member set differs")
        _digest(gen["checkpoint_manifest_sha256"])
        for record in gen["files"].values():
            self._record(record)
        if (
            sum(record["bytes"] for record in gen["files"].values())
            > self.plan["max_checkpoint_bytes"]
        ):
            _fail("Generation exceeds checkpoint size limit")
        if gen["files"]["manifest.json"]["sha256"] != gen["checkpoint_manifest_sha256"]:
            _fail("Generation does not bind original manifest")
        return gen

    def _generations(self):
        folder = self.root / "generations"
        names = sorted(
            name for name in os.listdir(folder) if not name.startswith(".partial-")
        )
        if len(names) > self.plan["max_generations"]:
            _fail("Generation count exceeds plan")
        result, parent = [], None
        for sequence, name in enumerate(names, 1):
            if name != f"{sequence:08d}.json":
                _fail("Generation ledger has a gap or unexpected member")
            raw = _read_path(folder / name, self.plan["max_generation_bytes"])
            gen = self._generation(raw, sequence, parent)
            parent = _sha(raw)
            result.append((parent, gen))
        return result

    def _bytes(self, record):
        raw = b"".join(
            self._blob(chunk["sha256"], chunk["bytes"]) for chunk in record["chunks"]
        )
        if len(raw) != record["bytes"] or _sha(raw) != record["sha256"]:
            _fail("Reconstructed member identity differs")
        return raw

    def audit(self):
        self._owned()
        if set(os.listdir(self.root_fd)) != {
            "writer.lock",
            "plan.json",
            "chunks",
            "generations",
            "staging",
            "restores",
        }:
            _fail("Unexpected archive root member")
        self._usage()  # Reject symlinks/devices everywhere, including crash residues.
        generations = self._generations()

        # A bounded per-audit cache avoids rereading shared prefixes thousands
        # of times. Never reuse integrity results across separate operations.
        @lru_cache(maxsize=max(1, (64 * 1024 * 1024) // self.plan["chunk_bytes"]))
        def checked_blob(digest, size=None):
            return self._blob(digest, size)

        for name in os.listdir(self.root / "chunks"):
            if not name.startswith(".partial-"):
                checked_blob(name)
        identity, before = None, None
        for digest, gen in generations:
            original_record = gen["files"]["manifest.json"]
            original_raw = b"".join(
                checked_blob(chunk["sha256"], chunk["bytes"])
                for chunk in original_record["chunks"]
            )
            if _sha(original_raw) != original_record["sha256"]:
                _fail("Archived original manifest hash differs")
            manifest = _json(original_raw)
            current = {
                key: manifest.get(key)
                for key in (
                    "run_directory",
                    "protocol_sha256",
                    "input_identity",
                    "source_pins",
                )
            }
            if identity is not None and _encode(current) != identity:
                _fail("Checkpoint run identity changed across generations")
            identity = _encode(current)
            if before is not None and (
                gen["steps"] <= before[0] or gen["at"] <= before[1]
            ):
                _fail("Generation cursors must strictly advance")
            before = gen["steps"], gen["at"]
            for record in gen["files"].values():
                h = hashlib.sha256()
                for chunk in record["chunks"]:
                    h.update(checked_blob(chunk["sha256"], chunk["bytes"]))
                if h.hexdigest() != record["sha256"]:
                    _fail("Archived member hash differs")
        # Restore residues may consume their dedicated reserve. They cannot make
        # another append legal; append always tests the stricter archive ceiling.
        used = self._usage()
        if used > self.plan["max_archive_bytes"] + self.plan["reserve_bytes"]:
            _fail("Archive plus recovery reserve exceeded")
        return {
            "generations": len(generations),
            "charged_bytes": used,
            "latest_sha256": generations[-1][0] if generations else None,
        }

    def archive_checkpoint(
        self,
        directory,
        *,
        expected_manifest_sha256,
        expected_parent_sha256=None,
        fault_hook=None,
    ):
        self._owned()
        self.audit()
        source = _canonical(directory)
        if (
            not _within(source, Path(self.plan["workspace_root"]))
            or source == self.root
            or _within(source, self.root)
        ):
            _fail("Source checkpoint must be outside archive and inside workspace")
        source_fd = _directory(source)
        try:
            manifest, records = _checkpoint(
                source_fd,
                self.plan,
                expected_manifest_sha256,
                verify_sources=self.plan["state"] == "SYNTHETIC_ONLY_APPEND_ONLY",
            )
            generations = self._generations()
            actual_parent = generations[-1][0] if generations else None
            if expected_parent_sha256 is not None:
                _digest(expected_parent_sha256)
            if actual_parent != expected_parent_sha256:
                _fail(
                    "Explicit externally pinned predecessor differs from archive head"
                )
            if len(generations) >= self.plan["max_generations"]:
                _fail("Generation limit exhausted; preserve evidence and stop")
            if generations:
                old = _json(self._bytes(generations[-1][1]["files"]["manifest.json"]))
                for key in (
                    "run_directory",
                    "protocol_sha256",
                    "input_identity",
                    "source_pins",
                ):
                    if _encode(old[key]) != _encode(manifest[key]):
                        _fail("Checkpoint run identity changed")
                if manifest["steps"] <= old["steps"] or manifest["at"] <= old["at"]:
                    _fail("Checkpoint completed cursor must strictly advance")
            gen = {
                "schema": 1,
                "state": "ARCHIVED_CHECKPOINT_NO_EXECUTION",
                "sequence": len(generations) + 1,
                "parent_sha256": generations[-1][0] if generations else None,
                "plan_sha256": self.plan_sha256,
                "checkpoint_manifest_sha256": expected_manifest_sha256,
                "at": manifest["at"],
                "steps": manifest["steps"],
                "files": records,
            }
            raw = _encode(gen)
            if len(raw) > self.plan["max_generation_bytes"]:
                _fail("Generation manifest exceeds pinned metadata limit")
            missing = {}
            for record in records.values():
                for chunk in record["chunks"]:
                    digest = chunk["sha256"]
                    if os.path.lexists(self.root / "chunks" / digest):
                        self._blob(digest, chunk["bytes"])
                    else:
                        missing[digest] = chunk["bytes"]
            # Conservative per-inode charges include publication staging;
            # no-replace rename requires no second data copy. Failed writes remain.
            extra = sum(
                _planned_charge(size) for size in missing.values()
            ) + _planned_charge(len(raw))
            self._capacity(extra)
            if fault_hook:
                fault_hook("after_admission")
            self._owned()
            for name, record in records.items():
                h = hashlib.sha256()
                with _member(source_fd, name) as stream:
                    for chunk in record["chunks"]:
                        data = stream.read(chunk["bytes"])
                        if len(data) != chunk["bytes"] or _sha(data) != chunk["sha256"]:
                            _fail("Source checkpoint changed during archival")
                        h.update(data)
                        if chunk["sha256"] in missing:
                            self._capacity(_planned_charge(len(data)))
                            self._publish("chunks/" + chunk["sha256"], data)
                            del missing[chunk["sha256"]]
                    if stream.read(1) or h.hexdigest() != record["sha256"]:
                        _fail("Source checkpoint changed during archival")
            if fault_hook:
                fault_hook("after_chunks")
            if fault_hook:
                fault_hook("before_generation_publication")
            self._owned()
            check, check_records = _checkpoint(
                source_fd,
                self.plan,
                expected_manifest_sha256,
                verify_sources=self.plan["state"] == "SYNTHETIC_ONLY_APPEND_ONLY",
            )
            if check != manifest or check_records != records:
                _fail("Source checkpoint changed before publication")
            self._capacity(_planned_charge(len(raw)))
            self._publish(f"generations/{gen['sequence']:08d}.json", raw)
            if fault_hook:
                fault_hook("after_generation_publication")
            self._owned()
            return {
                "sequence": gen["sequence"],
                "generation_sha256": _sha(raw),
                "checkpoint_manifest_sha256": expected_manifest_sha256,
                "charged_bytes": self._usage(),
                "synthetic": manifest["synthetic"],
                "execution_authorized": False,
            }
        finally:
            os.close(source_fd)

    def materialize(
        self, sequence, destination_name, *, expected_generation_sha256, fault_hook=None
    ):
        """Reconstruct exact checkpoint evidence under archive/restores. Never resume.

        Does not restore the original run path, copy a runtime, or resume replay.
        The named destination and all interrupted staging directories are retained.
        """
        self._owned()
        self.audit()
        if (
            not isinstance(destination_name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", destination_name) is None
        ):
            _fail("Safe exact materialization destination name required")
        generations = self._generations()
        if type(sequence) is not int or not 1 <= sequence <= len(generations):
            _fail("Explicit existing generation required")
        digest, gen = generations[sequence - 1]
        if digest != _digest(expected_generation_sha256):
            _fail("Pinned generation identity differs")
        if os.path.lexists(self.root / "restores" / destination_name):
            _fail("Restore destination is immutable and must be absent")
        extra = 2 * INODE_CHARGE + sum(
            _planned_charge(r["bytes"]) for r in gen["files"].values()
        )
        self._capacity(extra, restoring=True)
        temp_name = ".partial-" + uuid.uuid4().hex
        restores_fd = os.open(
            "restores",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self.root_fd,
        )
        temp_fd = None
        try:
            os.mkdir(temp_name, 0o700, dir_fd=restores_fd)
            temp_fd = os.open(
                temp_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=restores_fd,
            )
            os.mkdir("account", 0o700, dir_fd=temp_fd)
            for name, record in gen["files"].items():
                self._owned()
                parent_fd = (
                    os.open(
                        "account",
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=temp_fd,
                    )
                    if name.startswith("account/")
                    else os.dup(temp_fd)
                )
                try:
                    out = os.open(
                        name.split("/")[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o400,
                        dir_fd=parent_fd,
                    )
                    try:
                        h = hashlib.sha256()
                        with os.fdopen(out, "wb", closefd=False) as stream:
                            for chunk in record["chunks"]:
                                raw = self._blob(chunk["sha256"], chunk["bytes"])
                                stream.write(raw)
                                h.update(raw)
                            stream.flush()
                            os.fsync(out)
                        if h.hexdigest() != record["sha256"]:
                            _fail("Restored member hash differs")
                    finally:
                        os.close(out)
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            if fault_hook:
                fault_hook("after_restore_files")
            self._owned()
            manifest, records = _checkpoint(
                temp_fd,
                self.plan,
                gen["checkpoint_manifest_sha256"],
                verify_sources=False,
            )
            if (
                records != gen["files"]
                or manifest["steps"] != gen["steps"]
                or manifest["at"] != gen["at"]
            ):
                _fail("Restored checkpoint differs from generation")
            os.fsync(temp_fd)
            if fault_hook:
                fault_hook("before_restore_publication")
            self._owned()
            if os.path.lexists(self.root / "restores" / destination_name):
                _fail("Concurrent restore destination appeared")
            # Atomic no-replace also rejects an uncooperative racing publisher.
            _rename_noreplace(restores_fd, temp_name, restores_fd, destination_name)
            os.fsync(restores_fd)
            return {
                "path": str(self.root / "restores" / destination_name),
                "generation_sha256": digest,
                "checkpoint_manifest_sha256": gen["checkpoint_manifest_sha256"],
                "synthetic": manifest["synthetic"],
                "execution_authorized": False,
            }
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            os.close(restores_fd)
