"""One-off: clear route keys that survived closure on occurrence/gpt.

spec:protocol/occurrence.md §8 step 2 makes route removal part of closure.
Issues 005 and 006 are `Status: closed` in their READMEs but still routed in
package metadata, which check-commit reports as
`issue-routes/route-survives-closure`.

Metadata-only revision: `selector_fn` returns False for every entry, so the
existing versioned physical keys are reused and no bytes are copied.

    python scripts/clear_closed_routes.py --dry-run
    python scripts/clear_closed_routes.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys

PACKAGE = "occurrence/gpt"
REGISTRY = "s3://protology"
STALE_ROUTES = (
    "issues/005-executable-futurator-triadic-learning-machine",
    "issues/006-tlm-effect-test-realization-bridge",
)
MESSAGE = (
    "Clear the routes that survived closure on Issues 005 and 006. Both READMEs "
    "have been Status: closed since 2026-09-08 while package metadata still "
    "routed them, which spec:protocol/occurrence.md §8 step 2 makes part of "
    "closure and which check-commit reports as "
    "issue-routes/route-survives-closure. Metadata only: no entry is added, "
    "removed or rewritten, every immutable turn stays at its stable logical "
    "path, and the open Issue 010 route is left in place. Expected entry-count "
    "delta 0."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    import quilt3

    pkg = quilt3.Package.browse(PACKAGE, registry=REGISTRY)
    before = dict(pkg.meta)
    print(f"current tophash: {pkg.top_hash}")
    print(f"entries: {len(list(pkg.walk()))}")

    missing = [r for r in STALE_ROUTES if r not in before]
    if missing:
        print(f"nothing to do: routes already absent: {missing}", file=sys.stderr)
        if len(missing) == len(STALE_ROUTES):
            return 0

    after = {k: v for k, v in before.items() if k not in STALE_ROUTES}
    print("\n--- metadata before")
    print(json.dumps(before, indent=1, sort_keys=True))
    print("\n--- metadata after")
    print(json.dumps(after, indent=1, sort_keys=True))
    print(f"\n--- message\n{MESSAGE}")

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    pkg.set_meta(after)
    result = pkg.push(
        PACKAGE,
        registry=REGISTRY,
        message=MESSAGE,
        selector_fn=lambda logical_key, entry: False,  # reuse physical keys
        workflow="occurrence",
    )
    print(f"\npushed: {result.top_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
