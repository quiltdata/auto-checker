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
    os.environ.get("CHECKPASS_CACHE", pathlib.Path.home() / ".cache" / "checkpass")
)


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
            return RevisionView(
                tophash=tophash,
                pointer=pointer or d.get("pointer"),
                message=d.get("message") or "",
                meta=d.get("meta") or {},
                entries={
                    k: Entry(size=v.get("size"), hash=v.get("hash"), physical_key=v.get("pk"))
                    for k, v in d["entries"].items()
                },
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
        view = RevisionView(
            tophash=tophash,
            pointer=pointer,
            message=(pkg._meta or {}).get("message") or "",
            meta=pkg.meta or {},
            entries=entries,
        )
        cached.write_text(
            json.dumps(
                {
                    "pointer": pointer,
                    "message": view.message,
                    "meta": view.meta,
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
        data = self._fetch(entry)
        if data is not None:
            cached.write_bytes(data)
        return data

    def _fetch(self, entry: Entry) -> bytes | None:
        if not entry.physical_key:
            return None
        from urllib.parse import parse_qs, urlparse

        u = urlparse(entry.physical_key)
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
