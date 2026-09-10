# Auto-checker

Auto-checker continuously checks new Quilt package revisions against the policy for their package prefix. It receives Quilt `package-revision` events, runs deterministic Tier 0 checks, reports defects, and writes an anaimail response back to the package when findings require one.

Use this repository to put a package prefix such as `occurrence/*` or `myprefix/*` under automatic policy checking.

## How it works

Each deployment governs one package prefix:

```text
Quilt package revision
  -> EventBridge prefix filter
  -> SQS queue
  -> checker Lambda
  -> SNS findings and CloudWatch metrics
  -> anaimail response through the Quilt Packager, when needed
```

The package prefix controls both which revision events reach the checker and which policy file it loads. For example, a deployment with `packagePrefix=occurrence` checks `occurrence/*` packages with `src/check_commit/policies/occurrence.yaml`.

A missing policy is an engine error, never a silent pass.

## Prerequisites

You need:

- a Quilt stack in the target AWS account and region;
- the Quilt stack's Packager queue exports, `<quiltStackName>-PackagerQueueArn` and `<quiltStackName>-PackagerQueueUrl`;
- one or more registry buckets containing the packages to check;
- AWS credentials for the target account and region; and
- AWS CDK bootstrapped in that account and region.

The auto-checker stack must run in the same account and region as the Quilt stack whose Packager queue it uses.

## 1. Configure a prefix policy

Copy the worked example at `src/check_commit/policies/occurrence.yaml` to a file named for the prefix you want to govern:

```bash
cp src/check_commit/policies/occurrence.yaml src/check_commit/policies/myprefix.yaml
```

Edit the new file to describe conventions already established by the governed packages. The policy schema is `src/check_commit/policies/policy.schema.json`.

```yaml
# Registered checker identity used in revision metadata.
author: commit-protocol

# Registered cast label used in message filenames and From headers.
cast_label: CP

# Regexes for artifacts whose undeclared size decrease is a defect.
watchlist: []

# Word stems that count as declaring a size decrease.
decrease_markers: []

# Revision metadata fields that claim files were changed.
structured_file_fields: {}

# Historical folders exempt from the current filename form.
grandfathered_bare_folders: []

# Counter collisions already adjudicated by the governed corpus.
adjudicated_collisions: []
adjudication_cite: ""
```

`author`, `cast_label`, `watchlist`, and `structured_file_fields` are required by the schema. The lists and mapping may initially be empty. Register the checker identity and cast label in the governed protocol before enabling write-back, and cite package READMEs, closed issues, or other governing records when adding exceptions.

Policy controls corpus-specific behavior. Protocol-level rules—anaimail filename forms, issue paths, and `quilt+s3://` URI syntax—are built into the checker.

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
- `--json` emits a machine-readable report.
- `--offline` skips resolution of URIs that point outside the checked package.
- `check-commit compose <URI>` previews the response message without writing anything.

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
  --context region=<aws-region> \
  --context writeBack=true)
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

Notify-only mode is useful for evaluation or troubleshooting. Normal operation uses write-back once the checker's identity and cast label are registered for the governed prefix.

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

A clean revision is logged and needs no response. Findings are published to SNS and, with write-back enabled, rendered as an anaimail message and appended through the Quilt Packager. The checker recognizes and verifies its own response revision without generating a response loop.

Monitor the `CheckCommit` CloudWatch namespace and these alarms:

- `DefectsAlarm`
- `EngineErrorsAlarm`
- `SelfApplicationFailuresAlarm`

## Checks performed

| Check | What it detects |
| --- | --- |
| `delta-set` | Revision metadata that names files absent from the actual change set, or changed files omitted from declared metadata. |
| `watchlist-size` | Undeclared size decreases in policy-defined artifacts. |
| `filename-form` | Invalid anaimail filename forms and unadjudicated counter collisions. |
| `issue-paths` | Closed issues resurrected at vacated paths, or closure records removed incorrectly. |
| `uri-resolution` | Malformed or unresolved `quilt+s3://` references in changed documents. |
| `metadata-hygiene` | Stale inherited metadata that describes files untouched by the revision. |

Findings are classified as `defect` or `known-unresolved`. Policy-defined adjudications remain visible as known-unresolved rather than being silently ignored.

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

For the `occurrence` policy, `check-commit backtest` replays the pinned acceptance corpus and verifies the expected true positives and known-unresolved cases.

## Development

Run the test suite with `pytest -q`. The core engine and Lambda use the same policy loader and checks, so local CLI results exercise the same checking behavior used after deployment.

The design and operational background are maintained in the auto-checker project package, especially `05-auto-checker-stack.md` and `06-auto-checking-a-prefix.md`.

## Related Quilt packages

- [`proj/260810-auto-checker`](https://nightly.quilttest.com/b/quilt-dev/packages/proj/260810-auto-checker) — design and operational documentation
- [`occurrence/spec`](https://nightly.quilttest.com/b/quilt-ernest-staging/packages/occurrence/spec) — governing occurrence protocol and policy specifications
- [`marketing/ai-security`](https://nightly.quilttest.com/b/quilt-leadership/packages/marketing/ai-security) — related AI security guidance
