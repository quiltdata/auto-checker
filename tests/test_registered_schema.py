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
version was validated against the rules of its day. Comparing versions declared
five of nine `occurrence/*` packages defective while every one of them conformed
to the current schema.

So the comparison moves here — but be precise about what that buys, because the
first version of this file overstated it. Reading the registry needs
credentials, and the `unit` workflow supplies none, so in repository CI this
comparison *skips*. It gates the pre-deploy path, where credentials exist,
alongside the backtest the workflow already defers for the same reason.

Two things make the gap honest rather than hidden:

  - `CHECK_COMMIT_REQUIRE_REGISTRY=1` turns an unreachable registry from a skip
    into a failure, so the credentialed context that runs this before a deploy
    cannot pass by accident. `scripts/preflight.sh` sets it.
  - `test_vendored_schema_is_valid_and_pinned` needs no credentials and does run
    in CI. It cannot see registry drift, but it holds the vendored copy to being
    a well-formed schema that still says what the checker relies on, so an
    accidental edit fails the build rather than silently changing what
    `metadata-shape` validates against.
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

REQUIRE_REGISTRY = os.environ.get("CHECK_COMMIT_REQUIRE_REGISTRY") == "1"


@pytest.fixture
def policy():
    return Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence")


def _unavailable(reason: str):
    """Skip, or fail when the caller declared the registry must be reachable."""
    if REQUIRE_REGISTRY:
        pytest.fail(
            f"CHECK_COMMIT_REQUIRE_REGISTRY=1 but the registry comparison could "
            f"not run: {reason}"
        )
    pytest.skip(f"registry comparison did not run: {reason}")


def _registered():
    try:
        import boto3
    except ImportError:
        _unavailable("boto3 not installed")
    try:
        body = (
            boto3.client("s3")
            .get_object(Bucket=REGISTRY_BUCKET, Key=SCHEMA_KEY)["Body"]
            .read()
        )
    except Exception as exc:  # no credentials, no network, no bucket
        _unavailable(f"{type(exc).__name__}: {exc}")
    return json.loads(body)


# --- runs everywhere, credentials or not ------------------------------------

def test_vendored_schema_is_valid_and_pinned(policy):
    """The vendored copy is what `metadata-shape` validates every package's
    metadata against, so an accidental edit must fail the build even where the
    registry cannot be reached."""
    import jsonschema

    schema = json.loads(policy.vendored_schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)

    # `metadata-shape` reports `required` and `additionalProperties` violations
    # in §3's own words and suppresses the validator's version of them, which is
    # only sound while the two state the same rule. So the equivalence is pinned
    # here rather than assumed: `FIXED_META_FIELDS` against `required`, and
    # `ROUTE_KEY_RE` against `patternProperties`. If the schema moves and these
    # do not, the suppression would start hiding a real fault.
    from check_commit import policy as forms

    assert schema.get("additionalProperties") is False
    assert set(schema.get("required", [])) == set(forms.FIXED_META_FIELDS)
    assert forms.ROUTE_KEY_RE.pattern in schema.get("patternProperties", {})


# --- needs the registry ------------------------------------------------------

def test_vendored_schema_matches_the_registered_one(policy):
    vendored = json.loads(policy.vendored_schema_path.read_text())
    assert vendored == _registered(), (
        f"policies/{policy.vendored_schema} is out of date with "
        f"s3://{REGISTRY_BUCKET}/{SCHEMA_KEY}. Re-vendor it: the checker "
        f"validates package metadata against this copy, so a stale copy makes "
        f"metadata-shape wrong for every package."
    )
