# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-09-10

Retargets the deployment at the governed corpus in `s3://protology`, served by
the open catalog at <https://open.quiltdata.com>. The checked-in defaults still
named `quilt-ernest-staging` in account `712023778557`, which is not where the
corpus lives. See
[#17](https://github.com/quiltdata/auto-checker/issues/17).

### Changed

- CDK defaults name the open account: `account=867344438354`,
  `region=us-east-1`, `quiltStackName=open-quilt-bio`,
  `registryBuckets=protology`. `packagePrefix=occurrence` and
  `writeBack=false` carry over unchanged. `account` is now defaulted in
  `cdk.json` rather than left unset, so `cdk deploy` with no `--context` flags
  targets the intended account instead of whichever one the ambient credentials
  belong to.
- A notify-only stack no longer depends on the Packager queue. The
  `Fn.import_value` calls for `<quiltStackName>-PackagerQueueArn` and
  `-PackagerQueueUrl` are made only when `writeBack=true`, so the stack synths
  and deploys against a Quilt stack that does not export them. Previously a
  missing export was a synth failure regardless of whether write-back was
  enabled — the one dependency a notify-only deployment has no use for.
- A notify-only stack is granted no write access. `s3:PutObject` on
  `{prefix}/*` and `sqs:SendMessage` on the Packager queue are attached only
  when `writeBack=true`. Read grants (`s3:ListBucket`, `s3:GetObject`,
  `s3:GetObjectVersion` on `{prefix}/*` and `.quilt/*`) are unchanged. The
  Lambda could not legally use the write grants on this registry anyway: the
  write-back payload does not yet satisfy the registered workflow.
- `scripts/packager-roundtrip.py` defaults to `--stack-name open-quilt-bio` and
  `--bucket protology`, and its request is now one the registry admits: the two
  fields the registered schema requires, instead of the forbidden `author` and
  `delta` pair. On a validating registry the old payload was rejected outright,
  and a rejection is invisible from the probe — it surfaces only as the wait for
  the revision timing out. The verification step asserts the diff, the metadata
  round-trip, and the workflow stamp rather than running the full `occurrence`
  policy: a scratch package is not a governed one, so the protocol checks never
  applied to it, but the stamp is exactly the open Packager question, and this
  probe is how it gets answered.
- README documents the deployment context for the open account as a table of
  context keys and values, notes that the nine `occurrence/*` packages the
  prefix filter matches include four (`born`, `fixed`, `history`,
  `transcripts`) the catalog does not index, and points the corpus links at
  `open.quiltdata.com/b/protology`. The design-documentation links still name
  `nightly.quilttest.com`, which the retarget does not affect.
- README records that the two backtest corpora are in different accounts and
  that a stale `~/.cache/check-commit` can serve the old registry's views after
  a retarget.

### Verified

- The Packager queue exports are present in the open account:
  `open-quilt-bio-PackagerQueueArn` and `open-quilt-bio-PackagerQueueUrl`, both
  from stack `open-quilt-bio` in `us-east-1`. Write-back has a destination when
  it is enabled; it stays disabled for the contract reason below.
- `s3://protology/.quilt/workflows/config.yml` sets `is_workflow_required: True`
  with `default_workflow: occurrence`. Unlike `quilt-ernest-staging`, this
  registry validates every write, which is why `writeBack=false` is not merely
  the safe default here but the only correct setting until the contract work in
  [#16](https://github.com/quiltdata/auto-checker/issues/16) lands.
- `check-commit check "quilt+s3://protology#package=occurrence/spec"` runs
  against the open account from a developer machine: `PASS` at `d2b7cf60a91a`,
  regime `current`, 12 checks.
- `check-commit backtest --expectations backtest/expectations-current.yaml`
  passes against `protology` — 27 revisions, 11 required true positives, 5
  required false negatives.
- Both `writeBack` modes synth: notify-only emits no `Fn::ImportValue` and no
  `s3:PutObject`; `writeBack=true` emits both queue imports and the write
  grants.
- CDK is bootstrapped in `867344438354`/`us-east-1` (`CDKToolkit`).

### Unverified

- Whether the open Quilt stack emits `com.quiltdata` / `package-revision`
  events onto the default bus for `occurrence/*` writes. The ingress rule
  assumes it does; a deployment that receives no events is the symptom.
- The deployment itself. Nothing in this release has been applied to the open
  account, and `FindingsTopicArn` has no subscriber yet.

### Not addressed here

- The pre-existing `check-commit` deployment in `712023778557`/`us-east-1`,
  `UPDATE_COMPLETE` and last updated 2026-08-20, still watching
  `quilt-ernest-staging` against the `quilt-staging` Packager queue. Both that
  stack and the Quilt stack it depends on are live. This release retargets the
  checked-in defaults, not that deployment, so after it lands the two coexist:
  one checking the pre-migration registry, one checking `protology`. Retiring
  the staging deployment is a separate decision, and
  [#13](https://github.com/quiltdata/auto-checker/issues/13) is where the
  registry side of it belongs.

### Retained deliberately

- `backtest/expectations.yaml` keeps `registry: s3://quilt-ernest-staging` and
  its `occurrence/probability@7d74cc22a054` pin. `occurrence/probability` exists
  on `protology` too, but with a different revision history, so re-pinning would
  discard the adjudications the corpus records (`spec:issues/closed/030`,
  [#6](https://github.com/quiltdata/auto-checker/issues/6)) rather than move
  them. It is the only corpus that exercises the pre-migration checks, and it is
  reachable only while that registry stays live — which, verified against the
  staging account, it is: the pointers and the pinned manifest are both present
  and the full 166-revision backtest passes. Note the bucket is in `us-west-1`,
  not the `us-east-1` the rest of that account's stacks use.
- The hardcoded stack ID `check-commit` in `cdk/app.py`. It is not a prerequisite
  here. [#9](https://github.com/quiltdata/auto-checker/issues/9) is a collision
  within one account and region, and the pre-existing `check-commit` deployment
  is in `712023778557` while this one targets `867344438354`. Two live
  deployments of the same stack ID in different accounts is not a conflict; a
  second prefix in the open account would be.

## [0.3.0] - 2026-09-10

Re-bases the checker on the current `occurrence` contract. `occurrence/spec`
migrated twice: the 2026-08-13 metadata retirement left three of six checks
vacuous, and the folder model of `spec:protocol/occurrence.md` §5 then removed
the artifacts the replacement target named. The registered schema at
`s3://protology/.quilt/workflows/occurrence.json` now sets
`additionalProperties: false` and the registry sets `is_workflow_required`, so
every field the old checks read is forbidden rather than merely absent. See
[#16](https://github.com/quiltdata/auto-checker/issues/16).

### Added

- Regimes. A policy declares `regime: current | pre-migration` and each check
  declares which regimes authorize it, so a retired rule is never applied to a
  current write and a current rule is never applied to a corpus that predates
  it. `--regime` overrides the policy's choice; the report records which
  contract was applied.
- Ten checks for the current contract, none of which JSON Schema can express:
  - `workflow-stamp` — a revision written without the registered workflow, and
    therefore never validated.
  - `metadata-shape` — the three-field shape of §3, including any field the
    schema's `additionalProperties: false` forbids. This is the guard against
    the retired fields returning through an unvalidated `package_patch`.
  - `issue-routes` — route keys naming no issue in the manifest, and routes
    that survive closure (§8 step 2).
  - `issue-readme` — `Opened`/`Originator`/`Status` present, `Status` exactly
    `open|closed`, `Closed` and `Closed-By` present when closed, and a newly
    created README leading with its H1 (§5).
  - `turn-form` — `<issue>.<turn>-<contributor>-<slug>.md`, matching the
    containing folder, with no turn number taken twice.
  - `turn-immutability` — a filed turn whose bytes changed.
  - `entry-count` — the §4 duty, as arithmetic against the manifest rather than
    mention-matching against metadata prose. Also flags a claimed relocation
    that is not net zero, the signal that caught the `4ba6ce73` manifest loss
    recorded in `spec:issues/closed/041` Incident 1.
  - `pinned-citation` — cross-package evidence cited unpinned or at `@latest`
    (§7), with the current-guidance exception configured per prefix.
  - `key-drift` — logical keys backed at another physical path, or outside the
    registry bucket.
  - `schema-drift` — the package's and the registry's copies of the workflow
    schema against the one vendored here. §2 makes a stale schema a defect in
    its own right.
- The registered schema, vendored at
  `src/check_commit/policies/occurrence-workflow-schema.json`, byte-identical to
  both the registered object and the package's own copy at
  `protocol/occurrence-workflow-schema.json`.
- A second acceptance corpus, `backtest/expectations-current.yaml`, pinning
  `occurrence/spec@d2b7cf60` on `protology` — 27 revisions of the package that
  defines the contract, including the closure-metadata repair the route check is
  built for. `must_not_flag` asserts required false negatives, so a revision the
  contract clears cannot start failing unnoticed.
- The manifest workflow stamp is now carried on `RevisionView`, and
  `RevisionView.workflow_id` exposes it.

### Changed

- `delta-set`, `metadata-hygiene`, `filename-form`, and `issue-paths` are now
  pre-migration-regime checks. They read metadata fields the registered schema
  forbids and a filename grammar §5 retired, so they are unreachable against a
  current-regime package and sound only against the historical corpus (§9).
  They are retained rather than deleted because `backtest/expectations.yaml` is
  the only corpus that exercises them, and its adjudications
  (`spec:issues/closed/030`, `auto-checker#6`) are real rulings of the record.
- `backtest/expectations.yaml` declares `regime: pre-migration`. Its
  `known_unresolved` entries now name the check being adjudicated instead of
  relying on a hardcoded `filename-form` filter in the backtest runner.
- Write-back files a conforming issue turn at
  `issues/NNN-slug/NNN.TT-<contributor>-t0-check-of-<hash8>.md` — H1 first,
  provenance list immediately after — instead of an anaimail message with an
  envelope. There is no `Kind:`, no `Responds to`, and no `In-Reply-To`: folder
  membership establishes issue membership and the turn sequence establishes
  order.
- The Packager request omits package metadata entirely. Absent metadata
  preserves the parent's, whose `related_packages` and `status` carry forward
  already valid; the four fields it used to send are all forbidden now. The
  commit message carries the rationale and the entry-count claim, which is
  where §3 and §4 put them.
- The checker recognizes its own revisions by the shape of the write — one
  added turn with its own contributor label and slug — because §3 keeps
  revision attribution out of package metadata. The retired `author` metadata
  field is no longer consulted.
- `policies/occurrence.yaml` no longer references `protocol/anaimail.md` or
  `protocol/author_registry.yaml`, neither of which exists in the package, and
  no longer carries `cast_label` or `structured_file_fields` as live config.
  The retired tunables moved under `pre_migration:`, where only
  pre-migration-regime checks can reach them.
- The `occurrence` watchlist is now `protocol/occurrence.md`, the prefix's
  normative governing surface. The retired watchlist named message folders that
  no longer exist and is scoped to the pre-migration regime.
- Under the current regime, a declared reduction is read from the commit
  message only; the retired `delta` field declares nothing.
- A citation-form example in a protocol document is no longer read as a
  citation. `protocol/recruitment.md` shows the form as
  `quilt+s3://...#package=...@<revision>`, which both URI checks now skip.
- The report schema is `check-commit-report/1`, adding `regime`. The view cache
  is versioned, so entries written by an earlier engine are re-fetched rather
  than read back with no workflow stamp.
- `pytest` from the repository root now means the unit suite. It previously
  collected whatever a CDK asset bundle had vendored into `cdk/cdk.out`.

### Unverified

- Whether the Quilt Packager stamps `workflow: occurrence` on the revision it
  cuts. The queue contract carries no workflow field, so a stamped write depends
  on the Packager honouring the registry's `default_workflow`. If it does not,
  `workflow-stamp` fails on the checker's own revision and raises
  `SelfApplicationFailuresAlarm` rather than passing silently. Subscribe to that
  alarm before enabling write-back.

## [0.2.0] - 2026-08-20

### Changed

- Default the deployed checker to notify-only: `writeBack` in `cdk/cdk.json` is
  now `"false"`, matching the documented default. Findings go to SNS and
  CloudWatch; nothing is written to any package until write-back is explicitly
  enabled with `--context writeBack=true`.
- Rewrote the README as a task-oriented guide for putting a package prefix under
  automatic policy checking: how the event path works
  (`package-revision` -> EventBridge prefix filter -> SQS -> Lambda -> SNS
  findings -> Packager write-back), prerequisites, and numbered steps to
  configure a prefix policy, test it locally, build and deploy, subscribe to
  findings, and verify automatic checking.

### Added

- README documentation for prerequisites (Quilt stack Packager queue exports,
  registry buckets, CDK bootstrap, same-account/region constraint), the
  annotated prefix policy template, the CLI options `--policy`, `--json`,
  `--offline`, and `compose`, notify-only mode, the per-event log actions
  (`checked`, `self-applied`, `skipped`, `error`), the CloudWatch alarms
  (`DefectsAlarm`, `EngineErrorsAlarm`, `SelfApplicationFailuresAlarm`), the
  procedure for updating a deployed policy, and the stack-ID note for deploying
  more than one prefix per account and region.
- This changelog.

## [0.1.0] - 2026-08-11

### Added

- Initial `check-commit` release: a deterministic tier-0 checker for governed
  Quilt packages. No model, no inference — two revisions in, findings out.
- Six manifest-level checks: `delta-set`, `watchlist-size`, `filename-form`,
  `issue-paths`, `uri-resolution`, and `metadata-hygiene`, with findings
  classified as `defect` or `known-unresolved`.
- Per-prefix policy loading auto-selected from the package prefix, with a JSON
  schema at `src/check_commit/policies/policy.schema.json` and a worked example
  at `src/check_commit/policies/occurrence.yaml`. A prefix with no policy is an
  engine error, never a silent pass.
- CLI (`check-commit check`, `compose`, `backtest`) with exit codes `0` pass,
  `1` defects, `2` engine error.
- Backtest acceptance gate replaying every revision of
  `occurrence/probability` up to the pinned audit head against
  `backtest/expectations.yaml`.
- CDK stack deploying the EventBridge rule, SQS event queue and dead-letter
  queue, checker Lambda, prefix-scoped S3 permissions, SNS findings topic, and
  CloudWatch metrics and alarms, plus anaimail response write-back through the
  Quilt Packager queue.
- Operational scripts: `scripts/build-lambda.sh`, `scripts/sns.py`, and
  `scripts/packager-roundtrip.py`.

[0.3.1]: https://github.com/quiltdata/auto-checker/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/quiltdata/auto-checker/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/quiltdata/auto-checker/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/quiltdata/auto-checker/releases/tag/v0.1.0
