# Safe Spectra paper-state restoration

The restore utility is deliberately separate from the Alpha installer and
updater. It can be reviewed and invoked directly from the checked-out Core
repository; it does not modify the installed CLI or deployment scripts.

## Preconditions

- Spectra must be in paper-only mode:
  - `SPECTRA_PAPER_ONLY=true`
  - `SPECTRA_ALLOW_LIVE_EXECUTION=false`
- The archive must be a regular file under the installation's `backups/`
  directory.
- The configured paper and live state paths must be distinct.
- A fresh backup of the current paper state must be made before restore.
- Live state, credentials, .env files, and secrets are never restored.

## Run

From the Core repository root, after creating a fresh backup:

```bash
python3 scripts/spectra-alpha-restore.py \
  --install-root "$HOME/SpectraFinance-Alpha" \
  --archive "backups/<archive-name>.tar.gz"
```

The tool validates the archive structure and size, rejects links and
credential-like paths, validates restored SQLite databases, stages the new
state beside the target, and atomically swaps only the paper-state directory.
The previous paper state is retained in a sibling
`<state-name>.pre-restore-<id>` directory. Core is not restarted
automatically; inspect the result before manually restarting it.

This is a state-preservation utility, not a release updater. No installer,
updater, or live release switching is part of this change.
