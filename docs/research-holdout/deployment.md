# Public-data holdout capture deployment

The deployment uses one bounded, independent capture per daily request commit.
Each capture retrieves a fresh Kraken public AssetPairs snapshot and the recent
48-hour OHLC window for eligible USD currency pairs. Runs overlap; retain their
raw responses separately and assemble/deduplicate the evidence after
`2026-11-09T00:00:00Z`. A missed run is a recorded coverage gap, never a reason to
backdate the collection start or invent an earlier observation.

Kraken's [OHLC documentation](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
limits each reply to the latest 720 entries and always includes the incomplete
current interval. At five-minute resolution that is approximately 60 hours;
`since` cannot recover older missing history. The declared 48-hour daily
lookback provides overlap within that limit. The [AssetPairs
documentation](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
defines `aclass_base=currency` as spot currency pairs; tokenized assets are a
separate class.

This guide and `github-actions-template.yml` are documentation. The template
does not run from this directory. The separately deployed workflow, its branch,
and any automation request commits are the operational records of activation.
Creating a workflow file, scheduling an automation, or passing a public `/Time`
access probe does not establish that market capture started. The first actual
market response receipt establishes that fact.

## Trigger and run boundaries

The request producer commits a new file under
`research/holdout/requests/*.json` on
`research/parked-cash-workflow-20261009`. The capture
workflow reacts to that push and checks out the exact pushed commit SHA, so its
collector, request, and protocol can be traced to a concrete source revision.
It receives only `contents: read` through `GITHUB_TOKEN`. It does not need
exchange credentials, private exchange endpoints, account access, order
permissions, or repository write permission.

The deployed workflow is `.github/workflows/research-holdout-capture.yml`;
`github-actions-template.yml` is its byte-identical documentation copy. Its
repository guard is `andredepaulnash-cmd/spectra-finance-core`. It has no cron
trigger. Keep request commits small and dated; do not attach capture outputs to
Git commits.

`prepare_request.py` selects the latest dated request and verifies these exact
six fields before market access:

| Field | Required value |
| --- | --- |
| `schema` | `1` |
| `action` | `"public_ohlc_capture"` |
| `capture_date_utc` | UTC date matching the `YYYY-MM-DD.json` filename |
| `requested_utc` | Explicit UTC timestamp on that date; no more than 48 hours old or 90 seconds ahead |
| `protocol_sha256` | SHA-256 of the exact `research/holdout/frozen-protocol.json` bytes |
| `no_strategy_evaluation` | `true` |

Extra fields fail validation. The request receipt records its hash, the protocol
hash, checkout SHA, run ID, and attempt. The workflow runs mocked offline tests
before request validation, then bounded `poll`, metadata/hash `verify`, and
artifact upload. A valid request authorizes public capture only.

GitHub supports push workflows on branches that have not been merged into the
default branch. When both branch and path filters are configured, both must
match. A request producer running inside Actions cannot use that workflow's
`GITHUB_TOKEN` to cause another push-triggered workflow; GitHub normally
suppresses those recursive triggers. An independently authorized GitHub App or
user push is required for this pattern. See the official [push event
documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#push)
and [workflow trigger
documentation](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow).

Every run has its own empty output directory. It must not restore an earlier
capture, overwrite a prior artifact, or claim that its recent-window fetch is
an uninterrupted live stream. The collector makes sequential public requests
at approximately one request per 1.1 seconds, bounded by 800 requests and 1,200
seconds per run. Request limits and time limits can produce partial coverage;
the metadata must report that condition. No strategy performance, ranking,
profit, or outcome analysis is allowed before November 9.

## Protocol and strategy freeze

The committable `research/holdout/protocol.json` may be a
`DRAFT`/`NOT_STARTED` template while candidate strategy definitions are still
being completed. A template is not a prospective strategy freeze. The daily
collector refuses to fetch until it receives a real `FROZEN` protocol. The
actual source freeze and the first successful market capture are separate
timestamps; neither is backdated to the date the workflow was created.

To establish a prospective comparison, explicitly freeze the completed
candidate files once. The freeze command records its actual UTC execution time,
candidate SHA-256 hashes, collector SHA-256, and end boundary. Do not backdate
the freeze or run it afresh every day. Keep the original candidate files and
frozen receipt immutable. From the repository root, the command is:

```bash
python3 research/holdout/collect_public_ohlc.py freeze \
  --protocol research/holdout/protocol.json \
  --output initial-capture \
  --source-root . \
  --candidate baseline=research/holdout/candidates/baseline.json \
  --candidate experimental=research/holdout/candidates/experimental.json \
  --end 2026-11-09T00:00:00Z \
  --protocolout research/holdout/frozen-protocol.json
```

Candidate names and repository-relative paths in this example must be replaced
with the completed definitions. The output protocol path must not already
exist. `poll` requires a `FROZEN` protocol, nonempty candidate hash pins, and
matching candidate/collector source bytes. In an empty output directory it
copies that frozen protocol and creates a matching receipt with the original
freeze time; this does not create a new strategy freeze. A candidate frozen
after the data observation period begins is eligible only for the genuinely
unseen period after its actual freeze time.

Pin the complete financial candidate, including its parameters and the source
it imports. Hashing a small configuration or wrapper alone does not freeze
mutable imported strategy logic. Supply additional named pins or retain an
immutable complete source manifest/archive as part of the candidate definition.
Keep those repository-relative source bytes available at every deployed SHA.

For a new independent daily capture, run:

```bash
python3 research/holdout/collect_public_ohlc.py poll \
  --protocol research/holdout/frozen-protocol.json \
  --output capture \
  --source-root . \
  --max-requests 800 \
  --max-seconds 1200
python3 research/holdout/collect_public_ohlc.py verify --output capture
```

`poll` makes real public market requests when a valid frozen protocol is
present. Offline tests use mocked responses.

## Evidence retention and privacy

Upload every attempted capture with `if: always()`, including partial responses
and error metadata after collector failure. Give the artifact a distinct name
containing the run ID and attempt number. Upload only the capture directory,
not the checkout, environment files, credentials, or unrelated research data.
Raw response bodies are gzip files; console logs and workflow summaries contain
only status, timestamps, counts, source hashes, and integrity results. The
`verify` command checks metadata and raw hashes without computing outcomes.
The raw pages remain exact provider responses and can contain pre-start rows
or the mandatory current candle. They are sealed provenance. The stored candle
table excludes the last current/uncommitted row and applies the actual frozen
time window; that does not mean raw responses were stripped.

The template requests `retention-days: 90`. GitHub normally retains artifacts
and logs for 90 days, but this value cannot exceed the repository,
organization, or enterprise limit. Public repositories permit 1–90 days and
private repositories 1–400 days. Check the effective setting and archive all
successful and failed capture artifacts before expiry. Artifacts are not a
permanent archive: deleting a run/repository or storage failure can also remove
evidence. Prefer a configured private object store or another durable archive
for long-term retention; an Actions cache is not the evidence archive. See
[artifact retention](https://docs.github.com/en/actions/tutorials/store-and-share-data#configuring-a-custom-artifact-retention-period),
[organization retention limits](https://docs.github.com/en/organizations/managing-organization-settings/configuring-the-retention-period-for-github-actions-artifacts-and-logs-in-your-organization),
and the official [upload-artifact action](https://github.com/actions/upload-artifact).

Use a private repository or private external archive if the evidence must
remain private. Artifact access follows repository read access; calling the
inputs public market data does not make public-repository artifacts private.
Keep raw captures and any later analysis unreleased until the November 9
boundary, and preserve the restricted archive afterwards. See [downloading
workflow artifacts](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)
and [artifact API permissions](https://docs.github.com/en/rest/actions/artifacts).

## Operational checks before analysis

Review collection health using metadata alone. Confirm that each intended
daily request produced a run, that the pushed SHA matches the run, that an
artifact exists even for failed attempts, and that each downloaded capture
passes integrity verification. Record missing runs, time/request budget
exhaustion, request failures, and late collection explicitly. Repeated daily
AssetPairs snapshots preserve discovery of newly listed eligible USD pairs in
the raw collection.

The current financial comparison fixes membership in
`research/holdout/frozen-universe.json`, as declared by
`research/holdout/financial-spec.json`. Extra pairs found in later snapshots are
provenance/discovery only and never enter this comparison. Keep fixed members
in the audit when missing or untradeable; do not silently remove them or admit
later listings based on results. A future expanding-universe protocol would
need to predeclare a gate no earlier than each pair's first retained AssetPairs
observation; a 48-hour lookback cannot establish earlier causal membership.

Do not inspect raw OHLC values, run strategies, alter candidate rules based on
captured values, compare outcomes, or publish findings during the observation
period. On or after November 9, verify and assemble retained captures, preserve
their receipt times and source identities, deduplicate overlapping candles,
and report coverage limits alongside any comparison. Identical candles from
different receipts are overlapping evidence, not independent observations.

## Optional scheduling and stateful mode

There is no GitHub cron in the daily push design. If a future deployment uses
`17 * * * *`, that expression requests a run at minute 17 of every hour, using
UTC by default. GitHub scheduled workflows require the workflow on the default
branch and run only there, at its latest commit. Scheduled jobs can be delayed
and may be dropped under heavy load; avoiding minute zero reduces one common
load spike but does not guarantee execution. Public-repository schedules are
disabled after 60 days without repository activity. See the official [schedule
event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

A stateful hourly `poll` is a different deployment. Ephemeral runners would
need to restore the complete latest successful capture artifact from the named
workflow before each poll, compare its frozen protocol bytes with the deployed
protocol, then run `verify` before fetching. Fail closed if the latest state is
missing, expired, corrupt, or mismatched; never initialize a new freeze during
a recurring run. Keep failed-run artifacts for recovery review. GitHub's
[workflow-run API](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow),
[run-artifact API](https://docs.github.com/en/rest/actions/artifacts#list-workflow-run-artifacts),
and [GitHub CLI API command](https://cli.github.com/manual/gh_api) support explicit
restoration using a read-only Actions token. The daily snapshot template does
not implement this stateful alternative.

On an always-available local Linux machine, a systemd calendar timer is another
possible request producer or collector host. `Persistent=true` can cause one
catch-up activation when a calendar timer is reactivated after downtime; it
does not recreate every missed capture or guarantee collection while powered
off. Sleep, connectivity, and disk durability still affect coverage. No local
timer is installed by this guide. See the upstream [systemd timer
manual](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml).
