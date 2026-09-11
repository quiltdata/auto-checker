#!/usr/bin/env python3
"""CDK app for the check-commit tier-0 auto-checker (04 §6).

Inputs (CDK context, all defaulted in cdk.json):
  account          target AWS account (default 867344438354, the open account)
  region           target AWS region (default us-east-1)
  quiltStackName   Quilt CFN stack exporting the Packager queue (default open-quilt-bio)
  packagePrefix    governed package prefix (default occurrence)
  registryBuckets  comma-separated registry buckets (default protology)
  writeBack        "true" to enable issue-turn write-back (default false: notify-only)

The defaults target the governed corpus in s3://protology, served by the open
catalog at https://open.quiltdata.com. The auto-checker stack must land in the
same account and region as the Quilt stack whose Packager queue it uses.
"""

import aws_cdk as cdk

from stack import CheckCommitStack

app = cdk.App()

CheckCommitStack(
    app,
    "check-commit",
    env=cdk.Environment(
        account=app.node.try_get_context("account") or None,
        region=app.node.try_get_context("region") or "us-east-1",
    ),
)

app.synth()
