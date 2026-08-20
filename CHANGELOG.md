# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/quiltdata/auto-checker/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/quiltdata/auto-checker/releases/tag/v0.1.0
