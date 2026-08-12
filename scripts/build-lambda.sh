#!/usr/bin/env bash
# Build the Lambda asset directory without Docker: install check-commit and
# its deps as manylinux aarch64 wheels (the Lambda is arm64). boto3/botocore
# ship in the Lambda runtime and are excluded to keep the asset small.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/lambda
rm -rf "$OUT"
mkdir -p "$OUT"

python3 -m pip install . \
  --target "$OUT" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --quiet

# runtime provides boto3/botocore; strip them and pip metadata bulk
rm -rf "$OUT"/boto3 "$OUT"/botocore "$OUT"/*.dist-info/RECORD 2>/dev/null || true

du -sh "$OUT"
echo "lambda asset ready: $OUT"
