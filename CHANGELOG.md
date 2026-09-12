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
  `-PackagerQueueUrl` are made only when `writeBack=true`, so the stack deploys
  against a Quilt stack that does not export them. Previously the import was
  emitted regardless of whether write-back was enabled, and because
  `Fn.import_value` is a template-level intrinsic that synth emits unresolved,
  the failure landed at deployment as CloudFormation's "No export named ...
  found" — on the one dependency a notify-only deployment has no use for.
- A notify-only stack is granted no write access. `s3:PutObject` on
  `{prefix}/*` and `sqs:SendMessage` on the Packager queue are attached only
  when `writeBack=true`. Read grants (`s3:ListBucket`, `s3:GetObject`,
  `s3:GetObjectVersion` on `{prefix}/*` and `.quilt/*`) are unchanged. With
  write-back disabled the Lambda has no use for either grant, and holding them
  would be granting write access to a governed registry for no reason.
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
- README records that the two backtest corpora are in different accounts, and
  states what `~/.cache/check-commit` actually guarantees across a retarget:
  because entries are bucket-scoped and the revision list is fetched live, the
  old registry's views cannot be served after the switch.

### Verified

- The Packager queue exports are present in the open account:
  `open-quilt-bio-PackagerQueueArn` and `open-quilt-bio-PackagerQueueUrl`, both
  from stack `open-quilt-bio` in `us-east-1`. Write-back has a destination when
  it is enabled; it stays disabled for the reason below.
- `s3://protology/.quilt/workflows/config.yml` sets `is_workflow_required: True`
  with `default_workflow: occurrence`. Unlike `quilt-ernest-staging`, this
  registry validates every write. The payload itself is not the obstacle — 0.3.0
  already stopped sending package metadata, and absent metadata preserves the
  parent's, which validates. What keeps `writeBack=false` is that the Packager's
  stamping behaviour on a queue-requested write is unverified, and the contract
  work in [#16](https://github.com/quiltdata/auto-checker/issues/16) has not
  landed.
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

### Fixed

- The event queue's visibility timeout was below the checker's function timeout
  — 6 minutes against 10 — which Lambda rejects when it creates the event source
  mapping. Both now derive from one `CHECKER_TIMEOUT` constant, the queue at
  `VISIBILITY_RETRY_FACTOR` (6) times the function timeout, so they cannot drift
  apart again. That is 60 minutes of visibility against a 10-minute function.

  Six times, rather than the minimum Lambda enforces, because clearing the
  minimum only stops the mapping being rejected. With
  `reserved_concurrent_executions=1` a backlog leaves messages
  received-but-throttled, and if visibility expires while they wait they are
  redelivered and their receive count climbs toward `max_receive_count` on
  throttling alone — dead-lettering sound events during a burst. The wider window
  costs 3 hours to dead-letter a genuinely poisonous message, which an
  asynchronous findings pipeline can absorb.

  Both values date from the initial commit, so the stack has never been
  internally consistent, yet the staging deployment created its mapping without
  complaint in August. Why it was accepted then and refused now is not something
  this release establishes — the staging stack was deleted before the failure
  surfaced, so there is nothing left to inspect. Lambda validates the pair when
  the mapping is created, not when the function timeout changes, so any
  deployment carrying an already-created mapping would not have re-checked it.

### Deployed

- `check-commit` in `867344438354`/`us-east-1`, notify-only, against
  `s3://protology` and the `occurrence/` prefix.
  - The checker runs in Lambda and logs
    `{"action": "checked", "detail": "occurrence/spec@d2b7cf60a91a PASS"}` — the
    policy loads from the bundled asset, the read grants reach `protology`, and
    `quilt3` works with `HOME=/tmp`.
  - The ingress path delivers. An `occurrence/spec` `package-revision` event on
    the default bus passed the rule's `occurrence/` prefix filter, went through
    SQS, and produced a `checked` outcome in the log. The event was injected
    with `PutEvents`, not produced by a write to the corpus — see the
    outstanding criterion below.
  - The open Quilt stack does emit these events on the default bus, which was
    previously the unverified assumption behind the rule. `open-quilt-bio` runs
    its own `BenchlingPackageRevisionRule` on the same bus with the same
    `com.quiltdata` / `package-revision` pattern, and its target queue has
    received revision events every day for the past two weeks.
  - `FindingsTopicArn` has a confirmed email subscription.
  - The `CheckCommit` namespace is receiving `RevisionsChecked` and `Defects`.
    Both queues are empty and the DLQ has never held a message.

### Added (testing)

- Template assertions for the CDK stack, `tests/test_cdk_stack.py`, covering the
  failure classes this stack has actually produced. The visibility-timeout bug
  above is the motivating case: synth emits a valid template and Lambda rejects
  the mapping at deploy, so the cheap place to catch it is an assertion on the
  synthesized template. Both timeout invariants are asserted as relations rather
  than literals, and each test was checked by reintroducing the bug it covers and
  confirming it fails.
- The notify-only contract is now enforced rather than reviewed: no
  `s3:PutObject`, no `sqs:SendMessage` to the checker's role, no
  `Fn::ImportValue`, and an empty `PACKAGER_QUEUE_URL` when `writeBack` is off,
  with the write path reappearing when it is on. Also asserted: read grants
  scoped to `{prefix}/*` and `.quilt/*` rather than bucket-wide, the write grant
  scoped to `{prefix}/*`, the event pattern, reserved concurrency of one, and the
  three alarms.
- A `cdk` CI job that installs `aws-cdk-lib`, builds the Lambda asset, synthesizes
  the app, and runs those assertions. Synth alone catches a third class the
  assertions cannot: errors in stack construction, which is how a
  `Duration + Duration` `TypeError` surfaced while writing the timeout fix.
  Nothing here needs AWS credentials — account and region are explicit and the
  stack does no context lookups — so it runs on every push alongside the unit
  job.

  The assertions skip in the `unit` job, which does not install `aws-cdk-lib`, so
  the `cdk` job asserts `aws_cdk.assertions` imports before running them. A
  skipped test must not be able to pass for a green run.
- The assertions synthesize with the context from `cdk.json`, as the CDK CLI
  does, rather than against `App()` with none. `App` does not read `cdk.json`, so
  the first version of these tests asserted only the fallback defaults in
  `stack.py` — a `cdk.json` that enabled write-back against `protology` left all
  sixteen passing. Two tests now cover the deployment configuration directly: the
  checked-in context is asserted field by field, and `stack.py`'s fallbacks are
  required to produce the same template as `cdk.json`, so the two cannot drift
  into meaning different deployments depending on how the app was invoked.
- Workflow actions are pinned to full commit SHAs instead of major-version tags,
  in both jobs. A tag is mutable, so repointing `v4` would run replacement code
  on every push with no change to the workflow file. Each pin carries the release
  it was as a comment.
- `cdk/stack.py` resolves its Lambda asset from the module's own location instead
  of `../build/lambda` relative to the process's working directory, which only
  resolved when synth ran from `cdk/`. This is what makes the stack constructible
  from the test suite. The asset hash is content-based, so the template is
  unchanged and `cdk diff` against the deployed stack reports no differences.

### Outstanding

- No `checked` outcome from an organically written revision. #17 asks for the
  deployed rule to consume an event emitted by a real `occurrence/*` write, and
  that has not happened. The two halves are verified separately — the producer
  emits on the default bus, and the consumer processes an event placed on it —
  but not joined, so this release does not claim the deployment is verified end
  to end and #17 should stay open until a real write lands.

### Removed

- The `check-commit` deployment in `712023778557`/`us-east-1`, which had been
  live since 2026-08-20 watching `quilt-ernest-staging`. Deleted, making this a
  retarget rather than a second deployment: no `check-commit` stack now checks
  the pre-migration registry. Both queues were empty and the stack exported
  nothing, so nothing was lost and nothing depended on it. Its findings topic
  went with it, along with the confirmed email subscription — the open-account
  deployment needs its own via `scripts/sns.py subscribe`.

  The `quilt-staging` Quilt stack and its Packager exports are untouched, as is
  `s3://quilt-ernest-staging` and the pinned pre-migration corpus. The checker's
  Lambda log group, `/aws/lambda/check-commit-Checker1D892424-WdE6kI9lGrmh`,
  survives the stack deletion with its run history and no retention policy;
  delete it separately if that history is not wanted.

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

  A retarget cannot serve stale views from the old registry, contrary to the note
  in [#17](https://github.com/quiltdata/auto-checker/issues/17): the cache is
  namespaced by bucket and the revision list is fetched live, so a different
  registry means a different cache and a re-resolved history.
- The hardcoded stack ID `check-commit` in `cdk/app.py`.
  [#9](https://github.com/quiltdata/auto-checker/issues/9) is a collision within
  one account and region, and with the staging deployment deleted there is one
  deployment of this stack anywhere. It becomes a prerequisite when a second
  prefix is governed in the open account, not before.

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
