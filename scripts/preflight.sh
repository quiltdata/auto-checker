#!/usr/bin/env bash
# The gates that need registry credentials, which repository CI does not have.
#
# Two checks are deferred out of the `unit` workflow for the same reason: the
# backtest replays real revisions, and the registered-schema comparison reads
# `.quilt/workflows/occurrence.json`. Both are meaningless without read access
# to the registry bucket, and both skip silently when it is absent — which is
# how the schema comparison came to be described as gating a build it never ran
# in. `CHECK_COMMIT_REQUIRE_REGISTRY=1` makes an unreachable registry a failure
# here rather than a skip, so this script cannot pass by accident.
#
# Run before deploying:
#     bash scripts/preflight.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export CHECK_COMMIT_REQUIRE_REGISTRY=1
: "${CHECK_COMMIT_REGISTRY_BUCKET:=protology}"
export CHECK_COMMIT_REGISTRY_BUCKET

echo "== unit suite"
pytest -q

echo
echo "== registered schema (must be reachable)"
pytest tests/test_registered_schema.py -q

echo
echo "== backtest: pre-migration corpus"
check-commit backtest

echo
echo "== backtest: current corpus"
check-commit backtest --expectations backtest/expectations-current.yaml

echo
echo "preflight passed"
