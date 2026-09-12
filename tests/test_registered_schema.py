"""The vendored workflow schema tracks the registered one.

This is the duty that used to live in `check_commit.checks.check_schema_drift`
as `schema-drift/registered-schema-drift`, reported at `defect` severity
against whichever package revision happened to be checked. That was the wrong
subject. The finding's `paths` was always empty, because nothing in the package
was wrong: the comparison is between *this repo's* vendored copy and an object
in the registry, and neither side is something a package author wrote.

It also asserted more than the contract does. `spec:protocol/occurrence.md` §2
names the registered schema at an unversioned path, so "the registered schema"
can only mean whatever is registered now; a revision stamped with an older
version was validated against the rules of its day. Comparing versions
declared five of nine `occurrence/*` packages defective while every one of them
conformed to the current schema.

So the comparison moves here, where a stale vendored copy fails our build
instead of someone else's package. Network- and credential-dependent, so it
skips unless the registry is reachable.
"""

from __future__ import annotations

import json
import os

import pytest

from check_commit.policy import POLICY_DIR, Policy

# §2: the canonical location, unversioned. The current object at this key is
# the only thing §2 can mean by "the registered schema".
REGISTRY_BUCKET = os.environ.get("CHECK_COMMIT_REGISTRY_BUCKET", "protology")
SCHEMA_KEY = ".quilt/workflows/occurrence.json"


def _registered():
    boto3 = pytest.importorskip("boto3", reason="boto3 not installed")
    try:
        body = (
            boto3.client("s3")
            .get_object(Bucket=REGISTRY_BUCKET, Key=SCHEMA_KEY)["Body"]
            .read()
        )
    except Exception as exc:  # no credentials, no network, no bucket
        pytest.skip(f"registry unreachable: {type(exc).__name__}")
    return json.loads(body)


@pytest.mark.skipif(
    os.environ.get("CHECK_COMMIT_OFFLINE") == "1",
    reason="CHECK_COMMIT_OFFLINE=1",
)
def test_vendored_schema_matches_the_registered_one():
    policy = Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence")
    vendored = json.loads(policy.vendored_schema_path.read_text())
    assert vendored == _registered(), (
        f"policies/{policy.vendored_schema} is out of date with "
        f"s3://{REGISTRY_BUCKET}/{SCHEMA_KEY}. Re-vendor it: the checker "
        f"validates package metadata against this copy, so a stale copy makes "
        f"metadata-shape wrong for every package."
    )
