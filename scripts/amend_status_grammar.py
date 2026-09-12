"""One-off: amend spec:protocol/occurrence.md §5 to admit an annotated Status.

§5 said "Status is exactly `open | closed`", while three packages were writing
a state token followed by a running summary — `open — q10 finite classicality
unresolved`, `closed and promoted`. The practice is useful and the rule is
flat, so one of them had to move.

The rule moves, because the annotation carries real loop state and the flat
reading was also producing a silent failure: `_issue_status` compared the whole
field to "closed", so an annotated closure read as neither open nor closed and
`issue-routes/route-survives-closure` stopped firing. `occurrence/theory` Issue
068 is closed with provenance, still routed, and reported by nothing.

So §5 now specifies a state token plus an optional advisory annotation, and the
checker parses the leading token.

    python scripts/amend_status_grammar.py --body /tmp/occ.md --dry-run
    python scripts/amend_status_grammar.py --body /tmp/occ.md --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sys

PACKAGE = "occurrence/spec"
REGISTRY = "s3://protology"
LOGICAL_KEY = "protocol/occurrence.md"
MESSAGE = (
    "§5: admit an annotated Status. The field now specifies a state token, "
    "exactly open or closed, optionally followed by an advisory annotation that "
    "carries no protocol meaning; markdown emphasis around the token is ignored, "
    "and a reader must not infer closure from a 'closed' appearing later in the "
    "annotation. Recorded because three packages were already writing the "
    "annotation and it carries real loop state, while the flat reading was "
    "silently costing enforcement: comparing the whole field to 'closed' made an "
    "annotated closure read as neither state, so §8 route obligations stopped "
    "being checked on exactly the issues that had been closed. occurrence/theory "
    "Issue 068 is closed with provenance, still routed, and reported by nothing. "
    "protocol/occurrence.md grows 10879 -> 11989 bytes. Expected entry-count "
    "delta 0."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--body", required=True, help="the amended protocol/occurrence.md")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    body = pathlib.Path(args.body).read_bytes()
    if b"begins with a state token" not in body:
        print("error: --body does not carry the amendment", file=sys.stderr)
        return 2

    import quilt3

    pkg = quilt3.Package.browse(PACKAGE, registry=REGISTRY)
    before = pkg[LOGICAL_KEY].size
    print(f"current tophash: {pkg.top_hash}")
    print(f"{LOGICAL_KEY}: {before} -> {len(body)} bytes ({len(body) - before:+d})")
    print(f"entries: {len(list(pkg.walk()))} (unchanged)")
    print(f"\n--- message\n{MESSAGE}")

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    # Stage the amended bytes at the logical key's own physical path, so the
    # entry stays backed where it belongs.
    staged = pathlib.Path("/tmp/check-commit-staged") / LOGICAL_KEY
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(body)
    pkg.set(LOGICAL_KEY, str(staged))

    result = pkg.push(
        PACKAGE,
        registry=REGISTRY,
        message=MESSAGE,
        # Copy only the amended file; every other entry keeps its existing
        # versioned physical key.
        selector_fn=lambda logical_key, entry: logical_key == LOGICAL_KEY,
        workflow="occurrence",
    )
    print(f"\npushed: {result.top_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
