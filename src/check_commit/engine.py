"""Run the checks over one revision pair and produce a Report."""

from __future__ import annotations

import traceback

from . import __version__
from .checks import checks_for
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
        regime: str | None = None,
    ):
        self.history = history
        self.bucket = history.bucket
        self.package = history.package
        self.online = online
        self.policy = policy or Policy.for_package(history.package)
        # A corpus may be replayed against a regime other than the prefix's
        # own — that is what the pre-migration backtest does.
        self.regime = regime or self.policy.regime
        self._pairs = list(revision_pairs)
        self._tophashes = [t for _, t in self._pairs]
        self._foreign_cache: dict = {}
        self._object_cache: dict = {}
        self.notes: list[str] = []

    def content(self, view: RevisionView, path: str):
        return self.history.content(view, path)

    def read_s3_uri(self, uri: str):
        """A registry object by URI, memoized for the life of the context."""
        if uri not in self._object_cache:
            self._object_cache[uri] = self.history.read_s3_uri(uri)
        return self._object_cache[uri]

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    def resolve_same_package(self, tophash_prefix: str) -> RevisionView | None:
        """The revision a `@tophash` citation names, or None if it names none.

        The guard is against an *ambiguous prefix* — a short hash that could
        mean two different revisions — so it counts distinct manifests. Two
        pointers may name one top hash, because re-publishing identical content
        reuses the content hash and takes a fresh pointer, and counting pointer
        entries made a perfectly unambiguous citation unresolvable: a full
        64-character hash still matched "twice" and was reported as a revision
        that does not exist.
        """
        matches = {t for t in self._tophashes if t.startswith(tophash_prefix)}
        if len(matches) != 1:
            return None
        return self.history.view(matches.pop())

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
        regime=ctx.regime,
    )
    ctx.notes = report.notes
    # Two pointers may name one manifest: re-pushing identical content produces
    # the same top hash and a fresh pointer. `occurrence/theory` has six such
    # pairs. The revision then has nothing to compare itself against, so every
    # diff-scoped check is a no-op and only the whole-state checks say anything
    # — which is a true reading of a no-op re-push, but a reader looking at
    # `prev_tophash` would otherwise think a comparison had happened.
    if prev is not None and prev.tophash == cur.tophash:
        ctx.note(
            f"this revision re-publishes the manifest already at "
            f"{cur.tophash[:12]}, so it adds, removes and changes nothing. Checks "
            f"that read a diff have nothing to read; the whole-state checks "
            f"(metadata-shape, issue-routes, workflow-stamp, schema-drift) still apply"
        )
    for name, fn in checks_for(ctx.regime):
        try:
            report.findings.extend(fn(prev, cur, ctx))
            report.checks_run.append(name)
        except Exception:
            report.error = f"check {name} crashed:\n{traceback.format_exc()}"
            break
    return report
