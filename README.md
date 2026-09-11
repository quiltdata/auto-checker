# Auto-checker

Auto-checker continuously checks new Quilt package revisions against the policy for their package prefix. It receives Quilt `package-revision` events, runs deterministic Tier 0 checks, reports defects, and files an issue turn back into the package when findings require one.

Use this repository to put a package prefix such as `occurrence/*` or `myprefix/*` under automatic policy checking.

## How it works

Each deployment governs one package prefix:

```text
Quilt package revision
  -> EventBridge prefix filter
  -> SQS queue
  -> checker Lambda
  -> SNS findings and CloudWatch metrics
  -> issue turn through the Quilt Packager, when needed
```

The package prefix controls both which revision events reach the checker and which policy file it loads. For example, a deployment with `packagePrefix=occurrence` checks `occurrence/*` packages with `src/check_commit/policies/occurrence.yaml`.

A missing policy is an engine error, never a silent pass.

### Regimes

A governed corpus outlives the contract it was written under. Each policy declares a `regime`, and each check declares which regimes authorize it, so the engine never applies a retired rule to a current write or a current rule to a live package that has not migrated.

- `current` — the folder model of `spec:protocol/occurrence.md` §§2–8: three-field package metadata with an optional bounded routing namespace, stable issue folders, immutable turns, closure in place, and revision-pinned cross-package citation.
- `pre-migration` — the retired flat-issue and message-folder model. Its checks read metadata fields the registered schema now forbids, so they are sound only against the historical corpus (§9).

`--regime` overrides the policy's own choice, which is how the historical backtest corpus is replayed against the contract it was actually written under.

## Prerequisites

You need:

- a Quilt stack in the target AWS account and region;
- the Quilt stack's Packager queue exports, `<quiltStackName>-PackagerQueueArn` and `<quiltStackName>-PackagerQueueUrl`, for write-back only;
- one or more registry buckets containing the packages to check;
- AWS credentials for the target account and region; and
- AWS CDK bootstrapped in that account and region.

The auto-checker stack must run in the same account and region as the Quilt stack whose Packager queue it uses.

A notify-only deployment needs neither the Packager queue exports nor write access. With `writeBack=false` the stack does not import the exports and grants the Lambda no `s3:PutObject` or `sqs:SendMessage`, so it deploys against a Quilt stack that does not export a Packager queue at all. (A missing export is a deployment failure, not a synth failure: `Fn::ImportValue` is emitted unresolved and CloudFormation reports `No export named ... found`.)

### Deployment context: the `occurrence` corpus

The governed corpus lives in `s3://protology`, served by the open catalog at <https://open.quiltdata.com>. These are the checked-in defaults in `cdk/cdk.json`, so `cdk deploy` with no `--context` flags targets it:

| Context key | Value | Notes |
| --- | --- | --- |
| `account` | `867344438354` | the open account |
| `region` | `us-east-1` | |
| `quiltStackName` | `open-quilt-bio` | exports `open-quilt-bio-PackagerQueueArn` and `-PackagerQueueUrl` |
| `registryBuckets` | `protology` | |
| `packagePrefix` | `occurrence` | `occurrence/*` — nine packages, including four (`born`, `fixed`, `history`, `transcripts`) that carry named-package pointers the catalog does not index. The EventBridge prefix filter matches revision events for all of them. |
| `writeBack` | `false` | notify-only; see below |

Write-back stays off for this deployment. `s3://protology/.quilt/workflows/config.yml` sets `is_workflow_required: True` with `default_workflow: occurrence`, so the registry validates every write, and whether the Packager stamps that workflow on a revision it cuts from a queue request is still unverified. Enable write-back only after subscribing to `SelfApplicationFailuresAlarm`, which is where an unstamped self-write surfaces.

Deploying against a second registry in the same account and region needs a distinct stack ID first; see the note in [Build and deploy](#3-build-and-deploy).

## 1. Configure a prefix policy

Copy the worked example at `src/check_commit/policies/occurrence.yaml` to a file named for the prefix you want to govern:

```bash
cp src/check_commit/policies/occurrence.yaml src/check_commit/policies/myprefix.yaml
```

Edit the new file to describe conventions already established by the governed packages. The policy schema is `src/check_commit/policies/policy.schema.json`.

```yaml
# Which contract to check against: current | pre-migration.
regime: current

# Writer named in the commit message. Never written to package metadata.
author: commit-protocol

# Navigational label in the turn filenames this checker files. Human-readable
# metadata only: it carries no provenance authority and defines no job.
contributor: Checker

# The workflow id every revision must be stamped with, and the registered
# schema, vendored here so a stale deployment is itself a defect.
workflow: occurrence
package_schema_path: protocol/occurrence-workflow-schema.json
vendored_schema: occurrence-workflow-schema.json

# Regexes for artifacts whose undeclared size decrease is a defect.
watchlist: []

# Word stems that count as declaring a size decrease.
decrease_markers: []

# Packages that may be cited without a revision pin.
float_ok_packages: []

# Retired-regime tunables, read only by pre-migration checks. Not live config.
pre_migration: {}
```

`regime`, `author`, `workflow`, and `watchlist` are required by the schema; the lists may initially be empty. Cite package READMEs, closed issues, or other governing records when adding exceptions.

Vendor the registered workflow schema alongside the policy:

```bash
aws s3 cp s3://<registry-bucket>/.quilt/workflows/myprefix.json \
  src/check_commit/policies/myprefix-workflow-schema.json
```

Policy controls corpus-specific behavior. Protocol-level rules — issue folder and turn filename forms, the routing key namespace, and `quilt+s3://` URI syntax — are built into the checker.

## 2. Test the policy locally

Install the package and run its tests:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

Check the latest revision of a governed package:

```bash
check-commit check \
  "quilt+s3://<registry-bucket>#package=myprefix/some-package"
```

Check a specific revision by full hash or unique hash prefix:

```bash
check-commit check \
  "quilt+s3://<registry-bucket>#package=myprefix/some-package@<tophash>"
```

Useful options:

- `--policy path/to/policy.yaml` tests a policy before placing it under `policies/`.
- `--regime current|pre-migration` checks against a contract other than the policy's own.
- `--json` emits a machine-readable report.
- `--offline` skips resolution of URIs that point outside the checked package, and skips comparing the vendored schema against the registered one.
- `check-commit compose <URI>` previews the issue turn without writing anything.

Exit codes are `0` for pass, `1` for one or more defects, and `2` for an engine or configuration error. Known-unresolved findings are reported but do not produce a failing exit code.

## Bedrock Nemotron utility

`scripts/test_bedrock.py` discovers and prompts NVIDIA Nemotron models through Amazon Bedrock. It defaults to `us-east-1`; pass `--region` because model availability and pricing vary by region.

List matching foundation models and inference profiles:

```bash
python3 scripts/test_bedrock.py list nemotron
```

Prompt the largest active on-demand Nemotron model using an argument or stdin:

```bash
python3 scripts/test_bedrock.py prompt --max-tokens 256 "Summarize this design"
echo "Summarize this design" | python3 scripts/test_bedrock.py prompt
```

Automatic selection fails before invocation if the selected model and region do not have embedded pricing. Use `--model <model-id>` to pin a model rather than follow catalog changes. Add `--info` to write token usage, latency, request metadata, and estimated cost as JSON to stderr while leaving generated text on stdout:

```bash
python3 scripts/test_bedrock.py prompt \
  --model nvidia.nemotron-super-3-120b \
  --max-tokens 128 \
  --info \
  "Reply briefly"
```

`--max-tokens` defaults to 512 and caps generated tokens. Cost is an estimate based on embedded [Amazon Bedrock on-demand rates](https://aws.amazon.com/bedrock/pricing/), returned token counts, and service tier; it excludes taxes, negotiated discounts, and cache-specific pricing.

The caller needs `bedrock:ListFoundationModels` and `bedrock:ListInferenceProfiles` for discovery and `bedrock:InvokeModel` for prompting. Depending on account and model-access state, an administrator may also need to accept provider terms or grant AWS Marketplace subscription permissions. Prompt contents are sent to the Bedrock endpoint in the selected region and processed by the third-party NVIDIA model; do not submit sensitive data unless that path is permitted by your organization’s data-handling policy.

## 3. Build and deploy

Policy files ship inside the Lambda asset, so rebuild after every policy change:

```bash
bash scripts/build-lambda.sh

python3 -m venv .venv-cdk
.venv-cdk/bin/pip install -r cdk/requirements.txt

(cd cdk && ../.venv-cdk/bin/cdk deploy \
  --context packagePrefix=myprefix \
  --context registryBuckets=<bucket1>,<bucket2> \
  --context quiltStackName=<quilt-stack-name> \
  --context account=<aws-account-id> \
  --context region=<aws-region> \
  --context writeBack=true)
```

Every one of those keys is defaulted in `cdk/cdk.json`, so a deployment of the `occurrence` corpus described above is just:

```bash
(cd cdk && ../.venv-cdk/bin/cdk deploy)
```

This creates:

- an EventBridge rule for package revisions under `myprefix/`;
- an SQS event queue and dead-letter queue;
- a Lambda that loads `myprefix.yaml`;
- prefix-scoped S3 permissions;
- an SNS findings topic; and
- CloudWatch metrics and alarms.

The checker may upload its response file under the governed prefix, but it cannot write Quilt manifests. It asks the Quilt Packager to create the response revision.

The CDK app currently uses the stack ID `check-commit`. To deploy more than one prefix in the same account and region, first give each deployment a distinct stack ID in `cdk/app.py`, such as `check-commit-myprefix`.

### Notify-only mode

To check and alert without writing responses, deploy with:

```bash
(cd cdk && ../.venv-cdk/bin/cdk deploy --context writeBack=false ...)
```

Notify-only mode is useful for evaluation or troubleshooting, and it is the checked-in default. In this mode the stack drops the `s3:PutObject` and `sqs:SendMessage` grants and does not import the Packager queue exports, so it holds no write access to the governed registry and has no dependency it cannot use.

Write-back files one immutable issue turn per checked revision and sends no package metadata, so the parent's `related_packages` and `status` carry forward already valid. One dependency is unverified: the Packager queue contract carries no workflow field, so a stamped write depends on the Packager honouring the registry's `default_workflow`. If it does not, the `workflow-stamp` check fails on the checker's own revision and raises `SelfApplicationFailuresAlarm` rather than passing silently. Confirm that alarm is subscribed before enabling write-back.

## 4. Subscribe to findings

The deployment outputs `FindingsTopicArn`, `CheckerFunctionName`, and `EventQueueUrl`. Subscribe an operator to the findings topic:

```bash
python3 scripts/sns.py subscribe \
  --email you@example.com \
  --stack-name check-commit \
  --region <aws-region>
```

Confirm the email subscription, then inspect or remove subscriptions with:

```bash
python3 scripts/sns.py list --stack-name check-commit --region <aws-region>
python3 scripts/sns.py unsubscribe <subscription-arn> \
  --stack-name check-commit --region <aws-region>
```

## 5. Verify automatic checking

Push a revision to a package under the configured prefix and follow the checker logs:

```bash
aws logs tail /aws/lambda/<CheckerFunctionName> --follow --region <aws-region>
```

Each event produces a JSON outcome with one of these actions:

- `checked`: a governed revision was checked;
- `self-applied`: the Packager-created checker response was verified;
- `skipped`: the event was malformed or outside the configured prefix; or
- `error`: the checker could not complete the run.

A clean revision is logged and needs no response. Findings are published to SNS and, with write-back enabled, rendered as an issue turn and appended through the Quilt Packager. The checker recognizes its own revisions by the shape of the write — one added turn carrying its own contributor label and slug — because `spec:protocol/occurrence.md` §3 keeps revision attribution out of package metadata. It verifies that revision and files nothing in response, so there is no response loop.

Monitor the `CheckCommit` CloudWatch namespace and these alarms:

- `DefectsAlarm`
- `EngineErrorsAlarm`
- `SelfApplicationFailuresAlarm`

## Checks performed

Under the `current` regime:

| Check | What it detects |
| --- | --- |
| `workflow-stamp` | Revisions written without the registered workflow, whose metadata was therefore never validated. |
| `metadata-shape` | Missing `related_packages` or `status`, an invalid status, or any field the registered schema's `additionalProperties: false` forbids. |
| `issue-routes` | Route keys naming no issue in the manifest, and routes that survive closure. |
| `issue-readme` | Issue READMEs missing `Opened`, `Originator`, or `Status`, closed without `Closed` and `Closed-By`, or created without leading with their H1. |
| `turn-form` | Turn filenames that are not `<issue>.<turn>-<contributor>-<slug>.md`, name the wrong issue, or take a turn number already used. |
| `turn-immutability` | A filed turn whose bytes changed. Corrections are new turns. |
| `entry-count` | A commit message whose declared entry-count delta disagrees with the manifest, including a relocation that is not net zero. |
| `pinned-citation` | Cross-package evidence cited unpinned or at `@latest`. |
| `key-drift` | Logical keys backed at some other physical path, or outside the registry bucket. |
| `schema-drift` | The package's or the registry's copy of the workflow schema diverging from the vendored one. |
| `watchlist-size` | Undeclared size decreases in policy-defined artifacts. |
| `uri-resolution` | Malformed or unresolved `quilt+s3://` references in changed documents. |

Under the `pre-migration` regime, `watchlist-size` and `uri-resolution` still apply, joined by four checks of the retired contract: `delta-set`, `metadata-hygiene`, `filename-form`, and `issue-paths`.

Findings are classified as `defect` or `known-unresolved`. Policy-defined adjudications remain visible as known-unresolved rather than being silently ignored.

### What the checker does not check

The registered schema already enforces the shape of package metadata on every validated write, and `is_workflow_required` makes that validation mandatory. The checks above are the part of the contract JSON Schema cannot express: cross-revision arithmetic, artifact grammar, and the join between metadata, entry set, and file bytes.

## Updating a deployed policy

After changing a prefix policy:

```bash
pytest -q
bash scripts/build-lambda.sh
(cd cdk && ../.venv-cdk/bin/cdk deploy \
  --context packagePrefix=myprefix \
  --context registryBuckets=<bucket1>,<bucket2> \
  --context quiltStackName=<quilt-stack-name> \
  --context region=<aws-region> \
  --context writeBack=true)
```

For the `occurrence` policy, `check-commit backtest` replays a pinned acceptance corpus and verifies the expected true positives, required false negatives, and revisions that must come back clean. There are two corpora, one per regime, because a single pin cannot cover both contracts:

```bash
check-commit backtest --expectations backtest/expectations-current.yaml
check-commit backtest --expectations backtest/expectations.yaml
```

`expectations-current.yaml` pins `occurrence/spec` on `protology`, whose history contains the closure-metadata repair the route check is built for. `expectations.yaml` pins `occurrence/probability` before the 2026-08-13 metadata migration and declares `regime: pre-migration`, so the retired checks are exercised at full strength against the corpus they were written for.

Each corpus records its own `registry`, and the two are not in the same account: the current corpus reads `s3://protology` in the open account, and the pre-migration corpus reads `s3://quilt-ernest-staging`, which the retarget leaves in place as the only reachable home for those adjudications. Both need registry read credentials, so they run locally or pre-deploy rather than in CI, and the current-regime gate is the one that must pass before a deployment to the open account.

A stale local view cache can serve the old registry after a retarget. `check-commit` caches under `~/.cache/check-commit` keyed by bucket and package, so the retarget itself is safe, but clear it if a local run disagrees with the catalog.

## Development

Run the test suite with `pytest -q`. The core engine and Lambda use the same policy loader and checks, so local CLI results exercise the same checking behavior used after deployment.

The design and operational background are maintained in the auto-checker project package, especially `05-auto-checker-stack.md` and `06-auto-checking-a-prefix.md`.

## Related Quilt packages

The governed corpus is in `s3://protology` on the open catalog:

- [`occurrence/spec`](https://open.quiltdata.com/b/protology/packages/occurrence/spec) — governing occurrence protocol and the registered workflow schema
- [`occurrence/probability`](https://open.quiltdata.com/b/protology/packages/occurrence/probability), [`occurrence/theory`](https://open.quiltdata.com/b/protology/packages/occurrence/theory), [`occurrence/outcome`](https://open.quiltdata.com/b/protology/packages/occurrence/outcome), [`occurrence/gpt`](https://open.quiltdata.com/b/protology/packages/occurrence/gpt) — the checked corpus

Design documentation lives on a separate registry, unaffected by the retarget:

- [`proj/260810-auto-checker`](https://nightly.quilttest.com/b/quilt-dev/packages/proj/260810-auto-checker) — design and operational documentation
- [`marketing/ai-security`](https://nightly.quilttest.com/b/quilt-leadership/packages/marketing/ai-security) — related AI security guidance
