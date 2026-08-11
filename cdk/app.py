#!/usr/bin/env python3
"""CDK app for the check-commit tier-0 auto-checker (04 §6).

Inputs (CDK context, all defaulted):
  quiltStackName   Quilt CFN stack exporting the Packager queue (default quilt-staging)
  packagePrefix    governed package prefix (default occurrence)
  registryBuckets  comma-separated registry buckets (default quilt-ernest-staging)
  writeBack        "true" to enable anaimail write-back (default false: notify-only)

Region comes from the deploy environment (defaults to us-east-1 via cdk.json).
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
