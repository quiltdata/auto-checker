"""check-commit CLI: `check` one revision, or `backtest` the acceptance corpus."""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import sys

os.environ.setdefault("TQDM_DISABLE", "1")

from . import __version__
from .corpus import PackageHistory
from .engine import Context, run
from .model import DEFECT, KNOWN_UNRESOLVED
from .policy import Policy, PolicyError

URI_RE = re.compile(r"^quilt\+s3://(?P<bucket>[^#]+)#package=(?P<pkg>[^@&]+)(?:@(?P<hash>[^&]+))?")


def _history(args, package, bucket):
    return PackageHistory(package, bucket, cache_dir=args.cache and pathlib.Path(args.cache))


def cmd_check(args) -> int:
    m = URI_RE.match(args.uri)
    if not m:
        print(f"error: not a quilt+s3 package URI: {args.uri}", file=sys.stderr)
        return 2
    bucket, package, want = m.group("bucket"), m.group("pkg"), m.group("hash")

    history = _history(args, package, bucket)
    pairs = history.revisions()
    if not pairs:
        print(f"error: no revisions found for {package} in {bucket}", file=sys.stderr)
        return 2

    index = len(pairs) - 1
    if want and want != "latest":
        matches = [i for i, (_, t) in enumerate(pairs) if t.startswith(want)]
        if len(matches) != 1:
            print(f"error: revision {want!r} not found (or ambiguous)", file=sys.stderr)
            return 2
        index = matches[0]

    cur = history.view(pairs[index][1], pointer=pairs[index][0])
    prev = history.view(pairs[index - 1][1], pointer=pairs[index - 1][0]) if index else None

    try:
        pol = Policy.for_package(package, override=args.policy)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ctx = Context(history, pairs, online=not args.offline, policy=pol)
    report = run(prev, cur, ctx)

    if args.json:
        print(report.to_json())
    else:
        _print_report(report)
    return report.exit_code


def _print_report(report):
    print(f"check-commit {report.engine_version} — {report.package} @ {report.tophash[:12]}")
    print(f"  vs prev {report.prev_tophash[:12] if report.prev_tophash else '(none)'}")
    print(f"  verdict: {report.verdict.upper()}")
    if report.error:
        print(report.error)
        return
    for f in report.sorted_findings():
        print(f"  [{f.severity}] {f.check}/{f.kind}: {', '.join(f.paths)}")
        print(f"      {f.detail}")
    for n in report.notes:
        print(f"  note: {n}")


def cmd_backtest(args) -> int:
    import yaml

    exp_path = pathlib.Path(args.expectations)
    exp = yaml.safe_load(exp_path.read_text())
    package, registry = exp["package"], exp["registry"]
    bucket = registry.replace("s3://", "")
    pin = exp["pin"]

    history = _history(args, package, bucket)
    pairs = history.revisions()
    upto = [i for i, (_, t) in enumerate(pairs) if t == pin]
    if not upto:
        print(f"error: pin {pin[:12]} not found in revision list", file=sys.stderr)
        return 2
    pairs = pairs[: upto[0] + 1]
    print(f"backtest: {package}, {len(pairs)} revisions up to pin {pin[:12]}")

    try:
        pol = Policy.for_package(package, override=args.policy)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ctx = Context(history, pairs, online=args.online, policy=pol)
    by_hash: dict[str, list] = {}
    counts = collections.Counter()
    prev = None
    errors = []
    for ptr, tophash in pairs:
        cur = history.view(tophash, pointer=ptr)
        report = run(prev, cur, ctx)
        if report.error:
            errors.append((tophash, report.error))
        by_hash[tophash] = report.findings
        for f in report.findings:
            counts[(f.check, f.severity)] += 1
        prev = cur

    def findings_for(prefix):
        return [f for t, fs in by_hash.items() if t.startswith(prefix) for f in fs]

    failures = []

    for key, spec in exp["must_flag"].items():
        prefix = spec.get("hash", key)
        wanted = set(spec.get("checks", []))
        hits = [
            f
            for f in findings_for(prefix)
            if f.severity == DEFECT and (not wanted or f.check in wanted)
        ]
        status = "PASS" if hits else "FAIL"
        if not hits:
            failures.append(f"must_flag {key}: no {sorted(wanted) or 'defect'} finding")
        got = sorted({f"{f.check}/{f.kind}" for f in hits})
        print(f"  [{status}] must_flag {prefix:<10} ({spec.get('class', '')}): {got}")

    for spec in exp.get("known_unresolved", []):
        prefix, needle = spec["hash"], spec["paths_contain"]
        fs = [f for f in findings_for(prefix) if any(needle in p for p in f.paths)]
        ku = [f for f in fs if f.severity == KNOWN_UNRESOLVED]
        bad = [f for f in fs if f.severity == DEFECT and f.check == "filename-form"]
        ok = bool(ku) and not bad
        if not ok:
            failures.append(
                f"known_unresolved {prefix}: expected known-unresolved collision on "
                f"{needle}, got ku={len(ku)} defects={len(bad)}"
            )
        print(f"  [{'PASS' if ok else 'FAIL'}] known_unresolved {prefix} ({needle})")

    for tophash, err in errors:
        failures.append(f"engine error at {tophash[:12]}: {err.splitlines()[0]}")

    print("\nfindings by (check, severity):")
    for (check, sev), n in sorted(counts.items()):
        print(f"  {check:<20} {sev:<17} {n}")
    flagged = sum(1 for fs in by_hash.values() if any(f.severity == DEFECT for f in fs))
    print(f"revisions with defects: {flagged}/{len(pairs)}")

    if args.report:
        out = {
            t: [f.to_dict() for f in sorted(fs, key=lambda f: (f.check, f.kind, f.paths))]
            for t, fs in by_hash.items()
            if fs
        }
        pathlib.Path(args.report).write_text(json.dumps(out, indent=1, sort_keys=True))
        print(f"full findings written to {args.report}")

    if failures:
        print(f"\nBACKTEST FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nBACKTEST PASSED")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="check-commit", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="run the six T0 checks on one package revision")
    p.add_argument("uri", help="quilt+s3://<bucket>#package=<name>[@tophash]")
    p.add_argument("--offline", action="store_true", help="skip foreign-package URI resolution")
    p.add_argument("--json", action="store_true", help="emit the JSON report")
    p.add_argument("--cache", help="cache directory (default ~/.cache/check-commit)")
    p.add_argument("--policy", help="policy YAML (default: auto-selected by package prefix)")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("backtest", help="replay the acceptance corpus against expectations")
    p.add_argument(
        "--expectations",
        default=str(pathlib.Path(__file__).resolve().parents[2] / "backtest" / "expectations.yaml"),
    )
    p.add_argument("--online", action="store_true", help="also resolve foreign-package URIs")
    p.add_argument("--report", help="write full findings JSON to this path")
    p.add_argument("--cache", help="cache directory (default ~/.cache/check-commit)")
    p.add_argument("--policy", help="policy YAML (default: auto-selected by package prefix)")
    p.set_defaults(fn=cmd_backtest)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
