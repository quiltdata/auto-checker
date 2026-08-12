# auto-checker

Auto-checker for governed Quilt packages — deterministic T0 `check-commit` (proj/260810-auto-checker).

`check-commit` runs six manifest-level checks on one revision of a governed package
against its predecessor. No model, no inference: two revisions in, findings out,
verdict gated on accumulated check state. Spec: `proj/260810-auto-checker`
`04-mvp-tier-0-design.md` §4 (build spec `03` §1).

## Install

```bash
pip install -e .
```

Requires AWS credentials with read access to the registry bucket. Nothing else.

## Use

```bash
# check the head revision of a package
check-commit check "quilt+s3://quilt-ernest-staging#package=occurrence/probability"

# check a specific revision (any unique tophash prefix)
check-commit check "quilt+s3://quilt-ernest-staging#package=occurrence/probability@7d74cc22"

# machine-readable report
check-commit check "quilt+s3://..." --json
```

Exit codes: `0` pass (known-unresolved findings permitted and reported),
`1` one or more defects, `2` engine error. Never an unconditional success.

## The six checks

| Check | Finds |
| --- | --- |
| `delta-set` | revision metadata (`delta`, `adds`, `changes`, …) naming files the set omits, or set members nothing declares |
| `watchlist-size` | undeclared size decreases on watchlisted artifacts (policy from the package README) |
| `filename-form` | bare `NNL` names in `PARENT.NNL` folders; shared message counters |
| `issue-paths` | closed issues resurrected at their vacated `issues/` path; vacated closures |
| `uri-resolution` | `quilt+s3://` URIs in changed documents that do not resolve; pins that resolve to nothing |
| `metadata-hygiene` | metadata fields inherited verbatim from the prior revision that describe files this patch did not touch |

Severities: `defect`, `known-unresolved` (e.g. the counter collisions adjudicated
by `issues/closed/030` — reported, never silently passed, never a defect).

## Policy

Protocol-level forms (anaimail file naming, issue paths, URI syntax) live in the
engine. Everything specific to a governed corpus — watchlist, adjudicated
collisions, grandfathered folders, metadata field conventions — is **per-prefix
policy**, auto-selected from the package's prefix: `occurrence/probability`
loads [`src/check_commit/policies/occurrence.yaml`](src/check_commit/policies/occurrence.yaml).
The deployed stack's `packagePrefix` input selects the event filter and the
policy with the same value. A package whose prefix has no policy is an engine
error (exit 2), never a silent pass. Override with `--policy <file>`.

## Backtest — the acceptance gate

```bash
check-commit backtest
```

Replays every revision of `occurrence/probability` up to the pinned audit head
(`7d74cc22`) and asserts `backtest/expectations.yaml`: the named true positives
of `proj/260810-auto-checker` `03` §1 must be flagged, and the two adjudicated
counter collisions must surface as `known-unresolved`, not defects. Where `03`
cites a fix revision as evidence, the expectations file maps it to the
defective revision it documents.

The first run fetches revision views and changed-document contents into
`~/.cache/check-commit` (override with `--cache` or `CHECK_COMMIT_CACHE`);
subsequent runs are fast and offline for everything but the revision listing.

Per `04` §8, nothing deploys unless the backtest passes.

## Deployment (04 §6)

The deployed shape: EventBridge rule on the default bus (`com.quiltdata` /
`package-revision`, `detail.handle` prefix) → SQS (+DLQ) → one Lambda around
this same engine → SNS findings topic + CloudWatch alarms. Write-back goes
through the Quilt stack's Packager queue; the Lambda never writes manifests.

```bash
# 1. gates: unit tests + the credentialed backtest
pytest -q && check-commit backtest

# 2. build the Lambda asset (manylinux wheels, no Docker needed)
bash scripts/build-lambda.sh

# 3. synth / deploy (context defaults: quilt-staging, occurrence,
#    quilt-ernest-staging, writeBack=false)
python3 -m venv .venv-cdk && .venv-cdk/bin/pip install -r cdk/requirements.txt
cd cdk && cdk deploy   # add --context writeBack=true only after the cast-table entry exists
```

`writeBack=false` (the default) is **notify-only**: findings go to SNS and
metrics, nothing is written to any package. Flipping it on requires the
`CP` cast-table entry in the governed package (04 §10.2).

Operational scripts:

```bash
python3 scripts/sns.py subscribe --email you@example.com   # findings topic
python3 scripts/sns.py list
python3 scripts/packager-roundtrip.py                      # 04 §8 gate #4, standalone
```
