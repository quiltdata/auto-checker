"""Access to a package's revision history via quilt3, with a local cache.

quilt3 is the reader — manifests are never parsed by hand. The cache stores
the derived RevisionView (and file contents keyed by entry hash) so the
backtest is fast and repeatable after the first run. Content bytes are
fetched from the versioned physical key recorded by quilt3.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Iterable

from .model import Entry, RevisionView

DEFAULT_CACHE = pathlib.Path(
    os.environ.get("CHECK_COMMIT_CACHE", pathlib.Path.home() / ".cache" / "check-commit")
)

# Bump whenever a cached view gains a field, so stale entries are re-fetched
# rather than silently read back with the new field missing.
VIEW_CACHE_SCHEMA = 2


class PackageHistory:
    def __init__(self, package: str, bucket: str, cache_dir: pathlib.Path | None = None):
        self.package = package
        self.bucket = bucket
        self.registry = f"s3://{bucket}"
        base = pathlib.Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.cache = base / bucket / package.replace("/", "__")
        (self.cache / "views").mkdir(parents=True, exist_ok=True)
        (self.cache / "content").mkdir(parents=True, exist_ok=True)
        self._s3 = None

    # -- revision listing ------------------------------------------------

    def revisions(self) -> list[tuple[str, str]]:
        """All (pointer, tophash), oldest first, live from the registry."""
        import quilt3

        pairs = [
            (ptr, th)
            for ptr, th in quilt3.list_package_versions(self.package, registry=self.registry)
            if ptr != "latest"
        ]
        pairs.sort(key=lambda p: int(p[0]))
        return pairs

    # -- revision views ---------------------------------------------------

    def view(self, tophash: str, pointer: str | None = None) -> RevisionView:
        cached = self.cache / "views" / f"{tophash}.json"
        if cached.exists():
            d = json.loads(cached.read_text())
            # A view cached by an older engine has no workflow stamp recorded.
            # Treating that absence as "written without a workflow" would be a
            # false positive, so an old cache entry is a miss, not a hit.
            if d.get("schema") == VIEW_CACHE_SCHEMA:
                return RevisionView(
                    tophash=tophash,
                    pointer=pointer or d.get("pointer"),
                    message=d.get("message") or "",
                    meta=d.get("meta") or {},
                    entries={
                        k: Entry(size=v.get("size"), hash=v.get("hash"), physical_key=v.get("pk"))
                        for k, v in d["entries"].items()
                    },
                    workflow=d.get("workflow"),
                )

        import quilt3

        pkg = quilt3.Package.browse(self.package, registry=self.registry, top_hash=tophash)
        entries = {}
        for lk, entry in pkg.walk():
            h = entry.hash
            entries[lk] = Entry(
                size=entry.size,
                hash=h.get("value") if isinstance(h, dict) else h,
                physical_key=str(entry.physical_key),
            )
        manifest_meta = pkg._meta or {}
        workflow = manifest_meta.get("workflow")
        view = RevisionView(
            tophash=tophash,
            pointer=pointer,
            message=manifest_meta.get("message") or "",
            meta=pkg.meta or {},
            entries=entries,
            workflow=workflow if isinstance(workflow, dict) else None,
        )
        cached.write_text(
            json.dumps(
                {
                    "schema": VIEW_CACHE_SCHEMA,
                    "pointer": pointer,
                    "message": view.message,
                    "meta": view.meta,
                    "workflow": view.workflow,
                    "entries": {
                        k: {"size": e.size, "hash": e.hash, "pk": e.physical_key}
                        for k, e in entries.items()
                    },
                },
                sort_keys=True,
            )
        )
        return view

    # -- file contents ------------------------------------------------------

    def content(self, view: RevisionView, path: str) -> bytes | None:
        entry = view.entries.get(path)
        if entry is None:
            return None
        # entry hashes may be base64 (older manifests) — not filename-safe
        import hashlib

        key = hashlib.sha256((entry.hash or entry.physical_key or path).encode()).hexdigest()
        cached = self.cache / "content" / key
        if cached.exists():
            return cached.read_bytes()
        data = self.read_s3_uri(entry.physical_key) if entry.physical_key else None
        if data is not None:
            cached.write_bytes(data)
        return data

    def read_s3_uri(self, uri: str) -> bytes | None:
        """Bytes of an `s3://bucket/key[?versionId=...]` object, or None.

        Also serves objects outside any package: the registered workflow schema
        under `.quilt/workflows/` is named, version included, by the manifest's
        own workflow stamp.
        """
        from urllib.parse import parse_qs, urlparse

        u = urlparse(uri)
        if u.scheme != "s3":
            return None
        params = {}
        vid = parse_qs(u.query).get("versionId")
        if vid:
            params["VersionId"] = vid[0]
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        try:
            resp = self._s3.get_object(Bucket=u.netloc, Key=u.path.lstrip("/"), **params)
        except Exception:
            return None
        return resp["Body"].read()

    # -- lookup helpers ------------------------------------------------------

    def find_revision(self, prefix: str, pairs: Iterable[tuple[str, str]]) -> tuple[str, str] | None:
        matches = [(p, t) for p, t in pairs if t.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None
