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

`account` is pinned in cdk.json rather than left to the ambient credentials, so
a bare `cdk deploy` cannot land in whichever account happens to be configured.
That wiring lives in build_app() so it can be asserted; see
tests/test_cdk_stack.py.
"""

import aws_cdk as cdk

from stack import CheckCommitStack


def build_app(context: dict | None = None) -> cdk.App:
    """Construct the app. Context defaults to the CLI's (cdk.json plus flags)."""
    app = cdk.App(context=context)
    CheckCommitStack(
        app,
        "check-commit",
        env=cdk.Environment(
            account=app.node.try_get_context("account") or None,
            region=app.node.try_get_context("region") or "us-east-1",
        ),
    )
    return app


if __name__ == "__main__":
    build_app().synth()
