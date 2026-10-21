"""Lambda adapter around the check-commit engine.

The engine stays the deterministic core; this module is the deployment glue:
consume package-revision events (EventBridge -> SQS), route our own revisions
to self-application, run the checks on everything else, and act on the
verdict — notify always, write back only when enabled and safe.

Environment:
  PACKAGE_PREFIX           e.g. "occurrence" (selects policy; sanity-checks events)
  SNS_TOPIC_ARN            findings/alerts topic
  REGISTRY_URL             the Quilt registry that applies the patch (write-back)
  REGISTRY_API_KEY_SECRET  Secrets Manager secret holding its API key (write-back)
  WRITE_BACK               "true" to write response turns; anything else = notify-only
  METRIC_NAMESPACE         default "CheckCommit"

Notify-only is the safe default.
"""

from __future__ import annotations

import dataclasses
import json
import os

os.environ.setdefault("TQDM_DISABLE", "1")

from . import notify
from .compose import ComposeError, compose
from .corpus import PackageHistory
from .engine import Context, run
from .model import DEFECT, Report
from .policy import Policy
from .registry import ParentNotLatest, Registry


@dataclasses.dataclass
class Outcome:
    """What one event led to — returned for tests and logged for humans."""

    action: str  # checked | self-applied | skipped | error
    detail: str
    report: Report | None = None


class Handler:
    def __init__(self, env=None, s3=None, sns=None, cloudwatch=None, registry=None):
        e = env or os.environ
        self.prefix = e["PACKAGE_PREFIX"]
        self.topic_arn = e.get("SNS_TOPIC_ARN", "")
        self.registry_url = e.get("REGISTRY_URL", "")
        self.api_key_secret = e.get("REGISTRY_API_KEY_SECRET", "")
        self.write_back = e.get("WRITE_BACK", "").lower() == "true"
        self.namespace = e.get("METRIC_NAMESPACE", "CheckCommit")
        import boto3

        self.s3 = s3 or boto3.client("s3")
        self.sns = sns or boto3.client("sns")
        self.cloudwatch = cloudwatch or boto3.client("cloudwatch")
        self._registry = registry
        self.policy = Policy.for_package(f"{self.prefix}/_")

    def registry(self) -> Registry:
        # Read per write, not cached, so a rotated key takes effect at once.
        return self._registry or Registry.from_secret(self.registry_url, self.api_key_secret)

    # -- event plumbing -----------------------------------------------------

    def handle_sqs(self, event: dict) -> dict:
        """SQS batch entry point; reports partial batch failures."""
        failures = []
        for record in event.get("Records", []):
            try:
                detail = json.loads(record["body"]).get("detail", {})
                self.handle_detail(detail)
            except Exception as exc:  # noqa: BLE001 — one bad record must not sink the batch
                print(f"record failed: {exc}")
                failures.append({"itemIdentifier": record["messageId"]})
        return {"batchItemFailures": failures}

    def handle_detail(self, detail: dict) -> Outcome:
        handle, bucket, tophash = detail.get("handle"), detail.get("bucket"), detail.get("topHash")
        if not (handle and bucket and tophash):
            return self._log(Outcome("skipped", f"not a package-revision detail: {detail}"))
        if not handle.startswith(f"{self.prefix}/"):
            return self._log(Outcome("skipped", f"{handle} outside prefix {self.prefix}/"))

        history = PackageHistory(handle, bucket, cache_dir="/tmp/check-commit-cache")
        pairs = history.revisions()
        index = next((i for i, (_, t) in enumerate(pairs) if t == tophash), None)
        if index is None:
            return self._log(Outcome("error", f"revision {tophash[:12]} not found in {handle}"))
        cur = history.view(pairs[index][1], pointer=pairs[index][0])
        prev = history.view(pairs[index - 1][1], pointer=pairs[index - 1][0]) if index else None

        if self._is_own_revision(prev, cur):
            return self._self_apply(history, pairs, prev, cur, handle)

        is_head = index == len(pairs) - 1
        return self._check(history, pairs, prev, cur, handle, bucket, is_head)

    def _is_own_revision(self, prev, cur) -> bool:
        """Did this checker write the revision?

        Package metadata no longer carries revision attribution — §3 forbids
        it and the registered schema rejects it — so the signal is the write
        itself: exactly one added entry, nothing removed or changed (that is
        all we ever write), the entry is one of our turns, and the commit
        message is the one our patch carries.

        Both remaining signals are things any package writer could imitate;
        §5 leaves the contributor label navigational on purpose. Misrouting
        costs a response, not a wrong verdict — a revision taken for ours is
        verified rather than trusted, and a failure raises
        SelfApplicationFailures.
        """
        added, removed, changed = cur.diff(prev)
        return (
            len(added) == 1
            and not removed
            and not changed
            and self.policy.is_own_turn(added[0])
            and self.policy.authored_revision(cur.message)
        )

    # -- the two routes -----------------------------------------------------

    def _check(self, history, pairs, prev, cur, handle, bucket, is_head=True) -> Outcome:
        ctx = Context(history, pairs, online=True, policy=self.policy)
        report = run(prev, cur, ctx)
        self._metric("RevisionsChecked", 1)

        if report.error:
            self._notify(f"[check-commit] ERROR checking {handle}@{cur.tophash[:12]}", report.error)
            self._metric("EngineErrors", 1)
            return self._log(Outcome("error", report.error, report))

        defects = [f for f in report.findings if f.severity == DEFECT]
        self._metric("Defects", len(defects))
        if not report.findings:
            return self._log(Outcome("checked", f"{handle}@{cur.tophash[:12]} PASS", report))

        # The report as data goes to the log, where a machine consumer belongs;
        # the topic gets prose, because its subscriber is a person reading email.
        print(report.to_json())

        catalog_hint = f"{handle}@{cur.tophash}"
        if defects:
            self._notify(notify.subject(report), notify.render(report, self.policy))
        if not self.write_back:
            return self._log(
                Outcome("checked", f"{catalog_hint}: {len(report.findings)} finding(s), notify-only", report)
            )
        if not is_head:
            # a delayed/redelivered event for a superseded revision: composing
            # against its snapshot would allocate a counter the head may have
            # already claimed — notify only, never write from a stale view
            return self._log(
                Outcome(
                    "checked",
                    f"{catalog_hint}: {len(report.findings)} finding(s), stale event (head moved), notify-only",
                    report,
                )
            )
        return self._write_back(report, cur, prev, handle, bucket)

    def _write_back(self, report, cur, prev, handle, bucket) -> Outcome:
        slug = self.policy.response_slug(report.tophash)
        if any(slug in k for k in cur.entries):
            return self._log(Outcome("skipped", f"response for {slug} already filed", report))
        try:
            turn = compose(report, cur, prev, self.policy)
        except ComposeError as exc:
            self._notify(f"[check-commit] compose failed for {handle}", str(exc))
            return self._log(Outcome("error", f"compose: {exc}", report))

        # A redelivery stages and patches again: the registry does not retry.
        from quilt3.util import PhysicalKey

        key = f"{handle}/{turn.logical_key}"
        try:
            put = self.s3.put_object(Bucket=bucket, Key=key, Body=turn.text.encode())
            new_hash = self.registry().patch(
                bucket=bucket,
                name=handle,
                # Pinned: the turn number was allocated against this revision.
                parent=cur.tophash,
                entries={turn.logical_key: str(PhysicalKey(bucket, key, put.get("VersionId")))},
                # Rationale belongs in the commit message (§3), and the only
                # entry-count claim we can make is +1: one new turn.
                message=f"{self.policy.author}: T0 check of "
                f"{report.tophash[:12]} — {len(report.findings)} finding(s) filed at "
                f"{turn.logical_key}; expected entry-count delta +1",
                workflow=self.policy.workflow,
            )
        except ParentNotLatest as exc:
            # Same as a stale event: the new head gets its own check.
            return self._log(
                Outcome(
                    "checked",
                    f"{handle}@{cur.tophash}: {len(report.findings)} finding(s), stale event "
                    f"(head moved to {exc.latest[:12]} before the patch), notify-only",
                    report,
                )
            )
        except Exception as exc:
            self._notify(
                f"[check-commit] write-back FAILED for {handle}@{cur.tophash[:12]}",
                f"{turn.logical_key} was not confirmed filed: {exc}\n\n"
                "The event is returned to the queue; after three receives it is dead-lettered.",
            )
            self._metric("EngineErrors", 1)
            raise
        self._metric("ResponsesWritten", 1)
        return self._log(Outcome("checked", f"filed {turn.logical_key} in {handle}@{new_hash[:12]}", report))

    def _self_apply(self, history, pairs, prev, cur, handle) -> Outcome:
        """Our own patched revision came back: verify, never write.

        The patch names the policy's workflow, and the `workflow-stamp` check
        confirms the registry stamped it; a failure here raises
        SelfApplicationFailures.
        """
        added, removed, changed = cur.diff(prev)
        ctx = Context(history, pairs, online=False, policy=self.policy)
        report = run(prev, cur, ctx)
        if report.verdict not in ("pass", "known-unresolved"):
            print(report.to_json())
            self._notify(
                f"[check-commit] SELF-APPLICATION FAILED on {handle}@{cur.tophash[:12]}",
                f"The checker's own revision does not pass its own checks.\n\n"
                f"Diff: added={added} removed={removed} changed={changed}\n\n"
                f"{notify.render(report, self.policy)}",
            )
            self._metric("SelfApplicationFailures", 1)
            return self._log(Outcome("error", "self-application failed", report))
        return self._log(Outcome("self-applied", f"{handle}@{cur.tophash[:12]} verified", report))

    # -- side channels --------------------------------------------------------

    def _notify(self, subject: str, body: str):
        if self.topic_arn:
            self.sns.publish(TopicArn=self.topic_arn, Subject=subject[:100], Message=body[:262000])

    def _metric(self, name: str, value: float):
        self.cloudwatch.put_metric_data(
            Namespace=self.namespace,
            MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
        )

    @staticmethod
    def _log(outcome: Outcome) -> Outcome:
        print(json.dumps({"action": outcome.action, "detail": outcome.detail}, ensure_ascii=False))
        return outcome


_handler = None


def handler(event, context):  # Lambda entry point
    global _handler
    if _handler is None:
        _handler = Handler()
    return _handler.handle_sqs(event)
