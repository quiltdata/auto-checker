# auto-checker

Auto-checker for governed Quilt packages — deterministic T0 `checkpass` (proj/260810-auto-checker).

`checkpass` runs six manifest-level checks on one revision of a governed package
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
checkpass check "quilt+s3://quilt-ernest-staging#package=occurrence/probability"

# check a specific revision (any unique tophash prefix)
checkpass check "quilt+s3://quilt-ernest-staging#package=occurrence/probability@7d74cc22"

# machine-readable report
checkpass check "quilt+s3://..." --json
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

## Backtest — the acceptance gate

```bash
checkpass backtest
```

Replays every revision of `occurrence/probability` up to the pinned audit head
(`7d74cc22`) and asserts `backtest/expectations.yaml`: the named true positives
of `proj/260810-auto-checker` `03` §1 must be flagged, and the two adjudicated
counter collisions must surface as `known-unresolved`, not defects. Where `03`
cites a fix revision as evidence, the expectations file maps it to the
defective revision it documents.

The first run fetches revision views and changed-document contents into
`~/.cache/checkpass` (override with `--cache` or `CHECKPASS_CACHE`);
subsequent runs are fast and offline for everything but the revision listing.

Per `04` §8, nothing deploys unless the backtest passes.
