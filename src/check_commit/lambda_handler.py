"""Lambda adapter around the check-commit engine (04 §5–§6).

The engine stays the deterministic core; this module is the deployment glue:
consume package-revision events (EventBridge -> SQS), route self-authored
revisions to self-application, run the checks on everything else, and act on
the verdict — notify always, write back only when enabled and safe.

Environment:
  PACKAGE_PREFIX      e.g. "occurrence" (selects policy; sanity-checks events)
  SNS_TOPIC_ARN       findings/alerts topic
  PACKAGER_QUEUE_URL  the Quilt stack's Packager queue (write-back)
  WRITE_BACK          "true" to write anaimail responses; anything else = notify-only
  METRIC_NAMESPACE    default "CheckCommit"

Write-back stays off until the cast-table entry exists in the governed
package (04 §10.2); notify-only is the safe default.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re

os.environ.setdefault("TQDM_DISABLE", "1")

from .compose import ComposeError, compose
from .corpus import PackageHistory
from .engine import Context, run
from .model import DEFECT, Report
from .policy import Policy


@dataclasses.dataclass
class Outcome:
    """What one event led to — returned for tests and logged for humans."""

    action: str  # checked | self-applied | skipped | error
    detail: str
    report: Report | None = None


class Handler:
    def __init__(self, env=None, s3=None, sns=None, sqs=None, cloudwatch=None):
        e = env or os.environ
        self.prefix = e["PACKAGE_PREFIX"]
        self.topic_arn = e.get("SNS_TOPIC_ARN", "")
        self.packager_queue_url = e.get("PACKAGER_QUEUE_URL", "")
        self.write_back = e.get("WRITE_BACK", "").lower() == "true"
        self.namespace = e.get("METRIC_NAMESPACE", "CheckCommit")
        import boto3

        self.s3 = s3 or boto3.client("s3")
        self.sns = sns or boto3.client("sns")
        self.sqs = sqs or boto3.client("sqs")
        self.cloudwatch = cloudwatch or boto3.client("cloudwatch")
        self.policy = Policy.for_package(f"{self.prefix}/_")

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

        if (cur.meta or {}).get("author") == self.policy.author:
            return self._self_apply(history, pairs, prev, cur, handle)

        return self._check(history, pairs, prev, cur, handle, bucket)

    # -- the two routes -----------------------------------------------------

    def _check(self, history, pairs, prev, cur, handle, bucket) -> Outcome:
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

        catalog_hint = f"{handle}@{cur.tophash}"
        if defects:
            self._notify(
                f"[check-commit] {len(defects)} defect(s) in {handle}@{cur.tophash[:12]}",
                report.to_json(),
            )
        if not self.write_back:
            return self._log(
                Outcome("checked", f"{catalog_hint}: {len(report.findings)} finding(s), notify-only", report)
            )
        return self._write_back(report, cur, prev, handle, bucket)

    def _write_back(self, report, cur, prev, handle, bucket) -> Outcome:
        slug = f"t0-check-of-{report.tophash[:8]}"
        if any(slug in k for k in cur.entries):
            return self._log(Outcome("skipped", f"response for {slug} already filed", report))
        try:
            msg = compose(report, cur, prev, self.policy)
        except ComposeError as exc:
            self._notify(f"[check-commit] compose failed for {handle}", str(exc))
            return self._log(Outcome("error", f"compose: {exc}", report))

        key = f"{handle}/{msg.logical_key}"
        self.s3.put_object(Bucket=bucket, Key=key, Body=msg.text.encode())
        delta = f"SET CONTAINS EXACTLY: {msg.logical_key}. T0 findings for {report.tophash[:12]}."
        self.sqs.send_message(
            QueueUrl=self.packager_queue_url,
            MessageBody=json.dumps(
                {
                    "source_prefix": f"s3://{bucket}/{handle}/",
                    "package_name": handle,
                    "commit_message": f"{self.policy.author}: T0 check of {report.tophash[:12]} — "
                    f"{len(report.findings)} finding(s). delta: {msg.logical_key}",
                    # full metadata for our own post, every field true of THIS
                    # patch; set_meta replaces wholesale, so nothing inherited
                    "metadata": {
                        "author": self.policy.author,
                        "delta": delta,
                        "messages_added": [msg.logical_key],
                        "changes": [],
                    },
                }
            ),
        )
        self._metric("ResponsesWritten", 1)
        return self._log(Outcome("checked", f"wrote {key} and requested packaging", report))

    def _self_apply(self, history, pairs, prev, cur, handle) -> Outcome:
        """Our own Packager-cut revision came back: verify, never write."""
        added, removed, changed = cur.diff(prev)
        label = self.policy.cast_label
        ok_shape = (
            len(added) == 1
            and not removed
            and not changed
            and re.search(rf"\.\d{{2}}{label}-", added[0])
        )
        ctx = Context(history, pairs, online=False, policy=self.policy)
        report = run(prev, cur, ctx)
        if not ok_shape or report.verdict not in ("pass", "known-unresolved"):
            self._notify(
                f"[check-commit] SELF-APPLICATION FAILED on {handle}@{cur.tophash[:12]}",
                f"diff added={added} removed={removed} changed={changed}\n{report.to_json()}",
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
        print(json.dumps({"action": outcome.action, "detail": outcome.detail}))
        return outcome


_handler = None


def handler(event, context):  # Lambda entry point
    global _handler
    if _handler is None:
        _handler = Handler()
    return _handler.handle_sqs(event)
