# Bounded checkpoint-retention research candidate

## Scope and authority

`checkpoint_retention.CheckpointArchive` is a new, default-off research module.
It has no CLI and is not imported or invoked by `checkpoint.py`, `runner.py`,
`coordinator.py`, any operational path, or an admission/registry authority.
Constructing it requires `enabled=True`, an explicit plan file and the externally
pinned SHA-256 of that plan. It never simulates bars, resumes a run, edits an
existing checkpoint, moves a runtime, publishes results externally, deletes a
checkpoint, prunes evidence, or grants historical execution/recovery authority.

The default `SYNTHETIC_ONLY_APPEND_ONLY` plan admits only schema-2 completed
checkpoints with both `synthetic is True` and an explicit synthetic input identity.
A separate pinned `ARCHIVE_ONLY_NO_EXECUTION` plan additionally requires
`allow_non_synthetic_archival=True` at construction, on every opening. Only this
two-part opt-in permits preservation/materialization of non-synthetic checkpoint
bytes. It never authorizes execution; both append and materialization receipts
explicitly return `execution_authorized=False` and retain the synthetic label.

In either mode, the run directory must exactly equal the pinned plan. All seven
original member hashes, all three immutable SQLite integrity checks, caller
cursor/identity and paper clock are verified. The existing native format stores
paper clock values as floats, so exact numeric equality to the integer completed
cursor is accepted; booleans are rejected. Source-pin paths and hash commitments
are preserved/validated in both modes. Synthetic ingestion additionally verifies
current source files. Archive-only ingestion and all materializations do not
require old source paths still to exist: current source closure, protocol/registry
identity, accounting eligibility and recovery authority remain separate external
execution gates. Byte recovery does not establish that a run may resume.

Tests use artificial SQLite fixtures (including artificially relabelled
non-synthetic manifests to test the two-part archival gate) and the repository's
existing synthetic replay fixture. No historical dataset or operational
checkpoint is used by this candidate's implementation/testing work.

This candidate **does not reduce storage already occupied by original checkpoint
directories**: ingestion leaves them byte-for-byte intact. A future, separately
reviewed integration could write completed snapshots directly into this archive
or propose verified migration of redundant original copies. Neither integration
nor cleanup is implemented or authorized here.

## Exact evidence and generation ancestry

The current checkpoint schema pins these seven members:

- `account/paper.sqlite`, `account/native.sqlite`, `account/allocator.sqlite`
- `caller.json`, `events.jsonl`, `marks.jsonl`, `signals.jsonl`

The archive additionally preserves the exact original `manifest.json` bytes and
all present `-wal`, `-shm` and `-journal` sidecars for those three databases. Empty
files have explicit size/hash records with an empty chunk list. Unknown members,
symlinks, special files and hard-linked source members fail closed rather than
being omitted. Nonempty WAL/journal sidecars fail admission because the current
source manifest does not pin them as database authority. SHM bytes are retained
as evidence, never interpreted as authority. Source database validation opens
verified descriptors through Linux `/proc/self/fd` with SQLite `immutable=1`;
it does not create or rewrite WAL/SHM files in the source checkpoint.

Every admitted generation has an immutable, numbered JSON manifest containing:

- Exact plan identity and explicit generation sequence
- Exact predecessor generation-manifest hash, or null for genesis
- Exact original checkpoint-manifest hash and completed cursor
- Every member's whole-file SHA-256, length, and ordered chunk hash/length list

Chunks are fixed-size powers of two (4 KiB–4 MiB), addressed by SHA-256. Full
unchanged pages/prefixes deduplicate across members and generations. An append
changes at most the previous tail and new chunks for an append-only stream;
SQLite changed pages and shifted data may generate additional unique chunks.
Deduplication ratios depend on actual bytes and are not assumed by admission.

All published generations and chunks remain. The ledger cannot contain a gap,
an out-of-order cursor, a changed run/source/input/protocol identity, or a broken
predecessor link. Every append also requires the caller's externally retained
`expected_parent_sha256`, matching the archive's exact current head; null is
accepted only for genesis. This detects accidental head loss/truncation before a
new append. The archive itself does not establish independent durable authority:
the caller must retain plan and generation receipts outside the archive and must
not replace a missing receipt with a freshly computed guess.

## Capacity model: bounded admission, never evidence eviction

The plan has exact integer limits:

- `max_archive_bytes`: conservative charged archive ceiling
- `max_generations`: maximum committed immutable generation manifests
- `max_checkpoint_bytes`: maximum entire checkpoint, including manifest/sidecars
- `max_generation_bytes`: bounded generation-manifest size, at most 16 MiB
- `reserve_bytes`: separate recovery capacity, at least one maximum checkpoint
  plus 32 × 64 KiB of conservative metadata allowance

For each existing inode, charged storage is
`max(logical bytes, st_blocks × 512, 64 KiB)`; directory inodes also count.
Hard-linked in-archive regular-file aliases are counted once, though published
chunk/manifest readers reject multi-link files. Interrupted temporary files and
restore directories count, including unreferenced chunks. Planned new files are
charged as `64 KiB + round_up(logical bytes, 4096)` each. This intentionally
conservative allowance accommodates common filesystem allocation/metadata
rounding. The code checks full worst-case admission before writing any chunks,
then rechecks budget and available filesystem blocks before each publication.

Without recovery copies, the logical layout is approximately
`unique chunk bytes + all generation-manifest bytes + plan/control files`.
The charged bound additionally covers every inode and preserved interruption.
Ordinary appends stop before exceeding `max_archive_bytes`; materialization may
use up to `max_archive_bytes + reserve_bytes`. Materialization is preflighted for
all files and directories, and never deletes an earlier recovery attempt. Any
restored copy or crash residue also makes subsequent append admission stricter.
Repeated interrupted operations eventually stop rather than deleting evidence.

`statvfs` free-space checks protect the configured reserve at the instant of
admission; they do not physically preallocate storage or prevent unrelated
processes from consuming the disk. ENOSPC and other I/O failures preserve staged
and committed evidence and publish no incomplete generation. A hard physical
capacity guarantee on unusual filesystems or against external writers requires a
separately configured filesystem/project quota and reserved volume. This module
neither configures those nor claims that its conservative accounting is a kernel
quota. Sparse files are charged at least their logical size. POSIX permissions,
filesystem metadata and external mutations are outside a logical-byte theorem.

There is no bound on the amount of *new unique evidence a future campaign might
produce*. A finite plan therefore proves a safe stopping rule, **not campaign
completion**, a guaranteed generation count, or a fixed deduplication ratio.
Exhaustion is an explicit error requiring review; the module never silently
reduces retention, increases a limit, or chooses evidence to discard.

## Ownership, publication and recovery

A private mode-0700 archive root and private child directories are required.
A kernel `flock` grants one process exclusive ownership across both append and
materialization. The current PID, root inode, lock inode, child-directory inodes
and both plan copies are checked again at mutation boundaries. No stale-lock
takeover policy exists; the kernel releases ownership after actual process exit.
Paths must be canonical absolute paths without symlink aliases, and directory
components are opened with `O_NOFOLLOW`. Member names come from fixed allowlists;
restore names accept a single restricted component.

Chunk and generation publications write and fsync an exclusive temporary file,
then use Linux `renameat2(RENAME_NOREPLACE)` and fsync the containing directory.
No destination is overwritten, even by a competing publisher. Generation
publication occurs only after all referenced chunks are durable, source bytes
are checked again, and capacity/ownership still pass. There is no mutable HEAD
file whose interrupted update can hide a committed generation. Partial files and
unreferenced chunks are never promoted by discovery, silently removed or treated
as proof of successful publication.

`materialize(sequence, destination_name, expected_generation_sha256=...)` requires
an explicit existing generation and its externally retained hash. It verifies
ledger ancestry and chunk integrity, reconstructs each exact member into a new
private staging directory, checks whole-file hashes and the original checkpoint
semantics, fsyncs the result, and atomically publishes without replacement under
`archive/restores/<destination_name>`. It never writes the original run directory
or calls replay. Interrupted materializations remain `.partial-*` forensic data;
only an explicit newly named successful materialization is returned.

The trusted-local-filesystem model excludes a hostile process with the same UID
or root continuously rewriting files, kernel/volume dishonesty, and loss of the
entire local disk. Checks reject tested substitutions and corruption; they are
not a sandbox against an account owner who can rewrite code or permissions.
All chunks/manifests are read-only by convention/mode, not tamper-proof storage.
Every operation rechecks integrity rather than trusting an earlier operation;
a per-audit LRU cache is bounded to 64 MiB of chunk payloads plus bookkeeping.
Audits still inspect all generation/member evidence and need a separate
large-campaign performance assessment before operational integration. Shared chunk corruption fails closed and may affect several generations, so
independent durable replication remains a separate prerequisite for operational
recoverability. Original source files may be missing or changed during byte recovery; stored pin
commitments remain unchanged. Current source closure must be independently
verified before any later execution. The implementation requires Linux, `fcntl`, `/proc/self/fd`, `renameat2`, and a
filesystem honoring fsync and atomic no-replace rename. Unsupported platforms
fail rather than weakening publication.

## API sketch

```python
with CheckpointArchive(plan_path, expected_plan_sha256=plan_hash,
                       enabled=True) as archive:
    receipt = archive.archive_checkpoint(
        synthetic_checkpoint,
        expected_manifest_sha256=original_manifest_hash,
        expected_parent_sha256=previous_receipt_hash_or_none,
    )
    restored = archive.materialize(
        receipt['sequence'], 'synthetic_review',
        expected_generation_sha256=receipt['generation_sha256'],
    )
```

Plan state is `SYNTHETIC_ONLY_APPEND_ONLY`, or the separately opted-in
`ARCHIVE_ONLY_NO_EXECUTION` described above. Its schema is 1 and it must contain
exactly the fields validated by `PLAN_KEYS`; unknown or malformed limits,
boolean-for-integer coercions, duplicate JSON keys and nonfinite JSON fail.
An explicit plan is a resource/evidence policy, not user or trading authority.

## Verification

Run from the repository root with its test dependencies:

```bash
python -m pytest -q -s tests/test_weekly_checkpoint_retention.py \
    tests/test_weekly_checkpoint_retention_native.py
```

The deterministic 200-generation synthetic fixture reports full-copy logical
bytes, archive logical/allocated/charged bytes, unique chunks, budget/reserve and
exact reconstruction of generations 1, 37, 113 and 200. This is a modest storage
regression fixture, not a historical performance forecast. Separate tests cover
default-off opt-in and pin validation; source/manifest/file corruption; SQLite
sidecars and empty members; count/storage/free-space exhaustion; path traversal,
symlinks/hardlinks and lock replacement; actual competing processes; actual
process exits before/after publication; I/O failure with preserved residues;
source/plan changes (including a manifest double-read race); source-independent
byte recovery; separately opted-in archive-only mode; immutable/racing restore
destinations; broken ancestry and externally pinned head truncation detection. The native synthetic fixture proves
materialized bytes work with the existing checkpoint restore/resume contract and
reproduce all receipts, caller state, economics and caller logs exactly.
