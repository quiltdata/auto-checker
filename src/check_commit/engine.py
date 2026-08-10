"""Run the checks over one revision pair and produce a Report."""

from __future__ import annotations

import traceback

from . import __version__
from .checks import ALL_CHECKS
from .corpus import PackageHistory
from .model import Report, RevisionView
from .policy import Policy


class Context:
    """What checks may reach beyond the two revision views."""

    def __init__(
        self,
        history: PackageHistory,
        revision_pairs,
        online: bool,
        policy: Policy | None = None,
    ):
        self.history = history
        self.bucket = history.bucket
        self.package = history.package
        self.online = online
        self.policy = policy or Policy.for_package(history.package)
        self._pairs = list(revision_pairs)
        self._tophashes = [t for _, t in self._pairs]
        self._foreign_cache: dict = {}
        self.notes: list[str] = []

    def content(self, view: RevisionView, path: str):
        return self.history.content(view, path)

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    def resolve_same_package(self, tophash_prefix: str) -> RevisionView | None:
        matches = [t for t in self._tophashes if t.startswith(tophash_prefix)]
        if len(matches) != 1:
            return None
        return self.history.view(matches[0])

    def resolve_foreign(self, bucket: str, package: str, tophash, path):
        key = (bucket, package, tophash)
        if key not in self._foreign_cache:
            try:
                import quilt3

                pkg = quilt3.Package.browse(
                    package, registry=f"s3://{bucket}", top_hash=tophash
                )
                self._foreign_cache[key] = set(lk for lk, _ in pkg.walk())
            except Exception as exc:
                self._foreign_cache[key] = f"browse failed: {exc}"
        cached = self._foreign_cache[key]
        if isinstance(cached, str):
            return False, cached
        if path and path not in cached and not any(
            k.startswith(path.rstrip("/") + "/") for k in cached
        ):
            return False, f"path {path!r} absent"
        return True, ""


def run(
    prev: RevisionView | None,
    cur: RevisionView,
    ctx: Context,
) -> Report:
    report = Report(
        package=ctx.package,
        registry=ctx.history.registry,
        tophash=cur.tophash,
        pointer=cur.pointer,
        prev_tophash=prev.tophash if prev else None,
        engine_version=__version__,
    )
    ctx.notes = report.notes
    for name, fn in ALL_CHECKS:
        try:
            report.findings.extend(fn(prev, cur, ctx))
            report.checks_run.append(name)
        except Exception:
            report.error = f"check {name} crashed:\n{traceback.format_exc()}"
            break
    return report
