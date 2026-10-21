#!/usr/bin/env python3
"""Write-back round-trip probe — gate #4 of proj/260810-auto-checker 04 §8.

Exercises write-back's registry call against a scratch package, standalone:

  1. put one stray object under the test package's prefix, which the patch
     does not name and must not pick up
  2. put one small note there and patch it in, pinned to the current head
  3. assert the new revision is the parent plus exactly that note, every parent
     entry and its metadata recorded unchanged, its package metadata kept, and
     stamped with the policy's workflow

Needs s3:PutObject on the test prefix and a registry API key whose user can
write the bucket — by default the Secrets Manager secret the deployed Lambda
reads. Writes ONLY to the test package, which must already exist. Not a
pytest; run by hand before enabling write-back:

    PYTHONPATH=src python3 scripts/patch-roundtrip.py
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import tempfile

os.environ.setdefault("TQDM_DISABLE", "1")

import boto3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--registry-url", default="https://app-registry.quilt.bio")
    ap.add_argument("--api-key-secret", default="check-commit/registry-api-key")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--bucket", default="protology")
    ap.add_argument("--package", default="test/check-commit-roundtrip")
    args = ap.parse_args()

    import quilt3
    from quilt3.util import PhysicalKey

    from check_commit.corpus import PackageHistory
    from check_commit.policy import POLICY_DIR, Policy
    from check_commit.registry import Registry

    workflow = Policy.load(POLICY_DIR / "occurrence.yaml", prefix="occurrence").workflow
    history = PackageHistory(args.package, args.bucket, cache_dir=tempfile.mkdtemp())
    pairs = history.revisions()
    if not pairs:
        print(f"FAIL: {args.package} has no revision to patch; create it first")
        return 1
    prev_hash = pairs[-1][1]
    print(f"prior head: {prev_hash[:12]}")

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logical = f"notes/roundtrip-{ts}.md"
    s3 = boto3.client("s3", region_name=args.region)
    s3.put_object(Bucket=args.bucket, Key=f"{args.package}/notes/stray-{ts}.md", Body=b"not in the patch\n")
    key = f"{args.package}/{logical}"
    put = s3.put_object(Bucket=args.bucket, Key=key, Body=f"# Write-back round-trip probe {ts}\n".encode())
    print(f"put s3://{args.bucket}/{key} and a stray beside it")

    registry = Registry.from_secret(args.registry_url, args.api_key_secret, args.region)
    new_hash = registry.patch(
        bucket=args.bucket,
        name=args.package,
        parent=prev_hash,
        entries={logical: str(PhysicalKey(args.bucket, key, put.get("VersionId")))},
        message=f"commit-protocol: write-back round-trip probe {ts}; expected entry-count delta +1",
        workflow=workflow,
    )
    print(f"new head: {new_hash[:12]}")

    cur, prev = history.view(new_hash), history.view(prev_hash)
    failures = []
    # Compare recorded entries, not content: untouched entries must keep their
    # physical versions and hashes too.
    if set(cur.entries) - set(prev.entries) != {logical}:
        failures.append(f"added entries are not exactly the probe file: {sorted(set(cur.entries) - set(prev.entries))}")
    altered = [k for k, entry in prev.entries.items() if cur.entries.get(k) != entry]
    if altered:
        failures.append(f"parent entries removed or altered: {altered}")
    # RevisionView entries carry no entry metadata; read it from the manifests.
    old, new = (
        quilt3.Package.browse(args.package, registry=f"s3://{args.bucket}", top_hash=h) for h in (prev_hash, new_hash)
    )
    meta_changed = [k for k, entry in old.walk() if k in new and new[k]._meta != entry._meta]
    if meta_changed:
        failures.append(f"parent entry metadata changed: {meta_changed}")
    if cur.meta != prev.meta:
        failures.append(f"package metadata changed: {prev.meta} -> {cur.meta}")
    if cur.workflow_id != workflow:
        failures.append(f"revision stamped workflow {cur.workflow_id!r}, not {workflow!r}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS: write-back round-trip verified at {new_hash[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
