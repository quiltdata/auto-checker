#!/usr/bin/env bash
# Build the Lambda asset directory without Docker: install check-commit and
# its deps as manylinux aarch64 wheels (the Lambda is arm64). boto3/botocore
# ship in the Lambda runtime and are excluded to keep the asset small.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/lambda
rm -rf "$OUT"
mkdir -p "$OUT"

# setuptools stages the package into build/lib before wheeling it, and build_py
# copies a source file only when it is *newer* than the staged copy. A stale
# build/lib therefore silently ships old modules: a checkout or branch switch
# can leave src/ with an mtime behind the staging dir, and the wheel then
# carries whatever was staged last. This shipped a pre-0.3.0 checks/corpus/model
# to production under an 0.3.1 version string. Clearing the staging dirs is what
# makes the build a function of the source tree rather than of file timestamps.
rm -rf build/lib build/bdist.* 2>/dev/null || true

python3 -m pip install . \
  --target "$OUT" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --no-cache-dir \
  --quiet

# runtime provides boto3/botocore; strip them and pip metadata bulk
rm -rf "$OUT"/boto3 "$OUT"/botocore "$OUT"/*.dist-info/RECORD 2>/dev/null || true

# The asset is what actually runs, so prove it matches the source rather than
# trusting the build. cdk diff cannot catch this: it compares asset hashes, and
# a consistently-wrong asset hashes consistently.
stale=0
for f in src/check_commit/*.py; do
  packaged="$OUT/check_commit/$(basename "$f")"
  if [ ! -f "$packaged" ]; then
    echo "error: $(basename "$f") is missing from the asset" >&2
    stale=1
  elif ! cmp -s "$f" "$packaged"; then
    echo "error: $packaged differs from $f" >&2
    stale=1
  fi
done
if [ "$stale" -ne 0 ]; then
  echo "error: the built asset does not match src/check_commit; refusing to ship it." >&2
  echo "       remove build/ entirely and rebuild." >&2
  exit 1
fi

python3 - <<'PY'
import pathlib, re
src = pathlib.Path("src/check_commit/__init__.py").read_text()
want = re.search(r'__version__ = "([^"]+)"', src).group(1)
got = re.search(
    r'__version__ = "([^"]+)"',
    pathlib.Path("build/lambda/check_commit/__init__.py").read_text(),
).group(1)
assert want == got, f"asset version {got} != source {want}"
print(f"asset verified against src/check_commit (version {got})")
PY

du -sh "$OUT"
echo "lambda asset ready: $OUT"
