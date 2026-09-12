#!/usr/bin/env python3
"""Packager round-trip probe — gate #4 of proj/260810-auto-checker 04 §8.

Exercises the full write path against a scratch package, standalone:

  1. PutObject one small note under the test package's prefix
  2. send a PackagerEvent to the Quilt stack's exported Packager queue
  3. poll until the stack's Packager cuts the new revision
  4. assert the diff against the prior head is exactly the one file, the
     metadata round-tripped, and the revision carries the workflow stamp

Needs AWS credentials with s3:PutObject on the test prefix and
sqs:SendMessage on the Packager queue. Writes ONLY to the test package.
Not a pytest; run by hand or from a deploy pipeline:

    PYTHONPATH=src python3 scripts/packager-roundtrip.py

The defaults target the open account (s3://protology, Quilt stack
open-quilt-bio). That registry sets `is_workflow_required` with
`default_workflow: occurrence`, so the probe sends the two fields the registered
schema requires and nothing else. A request the registry rejects is invisible
from here: it surfaces as step 3 timing out, not as an error.

Step 4 checks the write path, not the protocol — a scratch package is not a
governed one. It asserts the diff, the metadata round-trip, and the workflow
stamp, the last being the open question about whether the Packager honours the
registry's default_workflow on a queue-requested write.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("TQDM_DISABLE", "1")

import boto3


def resolve_queue_url(stack_name: str, region: str) -> str:
    cfn = boto3.client("cloudformation", region_name=region)
    want = f"{stack_name}-PackagerQueueUrl"
    token = None
    while True:
        kwargs = {"NextToken": token} if token else {}
        resp = cfn.list_exports(**kwargs)
        for exp in resp["Exports"]:
            if exp["Name"] == want:
                return exp["Value"]
        token = resp.get("NextToken")
        if not token:
            raise SystemExit(
                f"error: export {want!r} not found in {region} — is {stack_name!r} "
                f"the right Quilt stack name?"
            )


def head_revision(package: str, registry: str):
    import quilt3

    pairs = [
        (int(p), t)
        for p, t in quilt3.list_package_versions(package, registry=registry)
        if p != "latest"
    ]
    return max(pairs)[1] if pairs else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stack-name", default="open-quilt-bio", help="Quilt CFN stack with the Packager export")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--bucket", default="protology")
    ap.add_argument("--package", default="test/check-commit-roundtrip")
    ap.add_argument("--timeout", type=int, default=180, help="seconds to wait for the Packager")
    args = ap.parse_args()

    registry = f"s3://{args.bucket}"
    queue_url = resolve_queue_url(args.stack_name, args.region)
    print(f"packager queue: {queue_url}")

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logical = f"notes/roundtrip-{ts}.md"
    key = f"{args.package}/{logical}"
    body = f"# Packager round-trip probe {ts}\n\nWritten by scripts/packager-roundtrip.py.\n"

    prev_hash = head_revision(args.package, registry)
    print(f"prior head: {prev_hash[:12] if prev_hash else '(no package yet)'}")

    s3 = boto3.client("s3", region_name=args.region)
    s3.put_object(Bucket=args.bucket, Key=key, Body=body.encode())
    print(f"put s3://{args.bucket}/{key}")

    # The registry validates every write (is_workflow_required), so the probe's
    # metadata has to be what the registered schema admits: the two required
    # fields and nothing else. The retired `author`/`delta` pair this probe used
    # to send is forbidden by additionalProperties: false, and a rejected
    # request is invisible from here — it would surface only as the wait below
    # timing out.
    metadata = {"related_packages": {}, "status": "active"}
    event = {
        "source_prefix": f"s3://{args.bucket}/{args.package}/",
        "package_name": args.package,
        "commit_message": f"commit-protocol: packager round-trip probe {ts}; "
        f"expected entry-count delta +1",
        "metadata": metadata,
    }
    boto3.client("sqs", region_name=args.region).send_message(
        QueueUrl=queue_url, MessageBody=json.dumps(event)
    )
    print("PackagerEvent sent; waiting for the stack to cut the revision...")

    deadline = time.time() + args.timeout
    new_hash = None
    while time.time() < deadline:
        time.sleep(5)
        h = head_revision(args.package, registry)
        if h and h != prev_hash:
            new_hash = h
            break
        print("  ...waiting")
    if not new_hash:
        print(f"FAIL: no new revision within {args.timeout}s")
        return 1
    print(f"new head: {new_hash[:12]}")

    # -- verify with check-commit's own machinery ---------------------------
    from check_commit.corpus import PackageHistory
    from check_commit.policy import POLICY_DIR, Policy

    with tempfile.TemporaryDirectory() as tmp:
        history = PackageHistory(args.package, args.bucket, cache_dir=tmp)
        cur = history.view(new_hash)
        prev = history.view(prev_hash) if prev_hash else None

        failures = []

        added, removed, changed = cur.diff(prev)
        if prev is not None and (added, removed, changed) != ([logical], [], []):
            failures.append(
                f"diff is not exactly the probe file: added={added} removed={removed} changed={changed}"
            )
        if logical not in cur.entries:
            failures.append(f"{logical} missing from the committed revision")
        if (cur.meta or {}) != metadata:
            failures.append(f"package metadata did not round-trip: {cur.meta}")

        # The probe checks the write path, not the protocol: a scratch package
        # is not a governed one, so the full policy does not apply to it. What
        # does apply to any revision, and is the open question this probe
        # answers, is whether the Packager stamped the workflow the registry
        # declares as its default. An unstamped write means write-back would
        # raise SelfApplicationFailures on its own revisions.
        want = Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence").workflow
        if cur.workflow_id == want:
            print(f"workflow stamp on committed revision: {want!r}")
        else:
            failures.append(
                f"revision stamped workflow {cur.workflow_id!r}, not the registry's "
                f"default {want!r}: the Packager does not stamp queue-requested "
                f"writes, so write-back cannot pass its own workflow-stamp check"
            )

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS: Packager round-trip verified at {new_hash[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
