"""The check-commit stack: rule -> SQS -> Lambda, SNS + alarms, minimal IAM.

Shape per proj/260810-auto-checker 04 §6; permissions per §7. The Lambda
holds no manifest-write access: package revisions are cut by the Quilt
stack's Packager, reached via its exported queue.

Write-back is the only reason this stack touches the Packager queue or holds
any write grant, so `writeBack` gates both. A notify-only deployment carries no
dependency on the queue exports and no ability to write anything.
"""

import pathlib

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    Fn,
    aws_cloudwatch as cw,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as sources,
    aws_sns as sns,
    aws_sqs as sqs,
)
from constructs import Construct

# One checker run: long enough to replay a package's revision history over the
# network. The event queue's visibility timeout is derived from it, because
# Lambda rejects a mapping where the queue would release a message while the
# function is still working on it.
CHECKER_TIMEOUT = Duration.minutes(10)
# AWS's recommended ratio of queue visibility timeout to function timeout.
VISIBILITY_RETRY_FACTOR = 6

# Resolved from this file, not the process's working directory, so the stack can
# be synthesized or asserted against from anywhere. `scripts/build-lambda.sh`
# writes here.
LAMBDA_ASSET = pathlib.Path(__file__).resolve().parent.parent / "build" / "lambda"


class CheckCommitStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs):
        super().__init__(scope, cid, **kwargs)

        ctx = self.node.try_get_context
        quilt_stack = ctx("quiltStackName") or "open-quilt-bio"
        prefix = ctx("packagePrefix") or "occurrence"
        buckets = (ctx("registryBuckets") or "protology").split(",")
        write_back = str(ctx("writeBack") or "false").lower()
        writes_enabled = write_back == "true"

        # The Packager queue is write-back's only destination, so a notify-only
        # deployment neither imports it nor is granted anything against it.
        # Fn.import_value is a template-level intrinsic: synth emits it
        # unresolved, and CloudFormation fails the deployment with "No export
        # named ... found" when it cannot resolve it. Importing unconditionally
        # would therefore make a notify-only deployment fail to deploy against a
        # Quilt stack that does not export the queue, rather than deploy fine
        # without write-back.
        packager_queue_arn = (
            Fn.import_value(f"{quilt_stack}-PackagerQueueArn") if writes_enabled else ""
        )
        packager_queue_url = (
            Fn.import_value(f"{quilt_stack}-PackagerQueueUrl") if writes_enabled else ""
        )

        # -- ingress: default-bus rule -> SQS (04 §3, §6) --------------------
        dlq = sqs.Queue(self, "DLQ", retention_period=Duration.days(14))
        # Six times the function timeout, per AWS's SQS/Lambda retry guidance.
        # Lambda's hard requirement is only >= CHECKER_TIMEOUT, but clearing the
        # minimum is not enough here: reserved_concurrent_executions=1 means a
        # backlog leaves messages received-but-throttled, and if visibility
        # expires while they wait, they are redelivered and their receive count
        # climbs toward max_receive_count on throttling alone. That would
        # dead-letter sound events during a burst. The cost of the wider window
        # is that a genuinely poisonous message takes 3 x 60 min to reach the
        # DLQ, which is acceptable for an asynchronous findings pipeline.
        queue = sqs.Queue(
            self,
            "Events",
            visibility_timeout=Duration.seconds(
                CHECKER_TIMEOUT.to_seconds() * VISIBILITY_RETRY_FACTOR
            ),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )
        events.Rule(
            self,
            "PackageRevisions",
            description=f"Quilt package-revision events for {prefix}/*",
            event_pattern=events.EventPattern(
                source=["com.quiltdata"],
                detail_type=["package-revision"],
                detail={"handle": [{"prefix": f"{prefix}/"}]},
            ),
            targets=[targets.SqsQueue(queue)],
        )

        # -- egress: findings topic ------------------------------------------
        topic = sns.Topic(self, "Findings", display_name=f"check-commit {prefix} findings")

        # -- the checker -------------------------------------------------------
        fn = lambda_.Function(
            self,
            "Checker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            code=lambda_.Code.from_asset(str(LAMBDA_ASSET)),
            handler="check_commit.lambda_handler.handler",
            timeout=CHECKER_TIMEOUT,
            memory_size=1024,
            # one revision at a time: serializes checks and counter allocation
            reserved_concurrent_executions=1,
            environment={
                # quilt3 writes config/cache under HOME, which is read-only in
                # Lambda; /tmp is the only writable filesystem
                "HOME": "/tmp",
                "XDG_CACHE_HOME": "/tmp/xdg-cache",
                "XDG_CONFIG_HOME": "/tmp/xdg-config",
                "PACKAGE_PREFIX": prefix,
                "SNS_TOPIC_ARN": topic.topic_arn,
                "PACKAGER_QUEUE_URL": packager_queue_url,
                "WRITE_BACK": write_back,
                "QUILT_STACK_NAME": quilt_stack,
                "TQDM_DISABLE": "1",
            },
        )
        fn.add_event_source(
            sources.SqsEventSource(queue, batch_size=1, report_batch_item_failures=True)
        )

        # -- permissions (04 §7): read prefix + .quilt, put message files only,
        # send to the Packager, publish findings, emit metrics ------------------
        bucket_arns = [f"arn:aws:s3:::{b}" for b in buckets]
        fn.add_to_role_policy(
            iam.PolicyStatement(actions=["s3:ListBucket"], resources=bucket_arns)
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                # quilt3 reads manifests and entry bytes by versionId
                actions=["s3:GetObject", "s3:GetObjectVersion"],
                resources=[f"{a}/{p}" for a in bucket_arns for p in (f"{prefix}/*", ".quilt/*")],
            )
        )
        if writes_enabled:
            # Staging a turn file and asking the Packager to cut the revision are
            # both write-back steps. Notify-only gets neither: the Lambda would
            # not be able to use the grants legally anyway.
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    sid="WriteOwnMessagesOnly",
                    actions=["s3:PutObject"],
                    resources=[f"{a}/{prefix}/*" for a in bucket_arns],
                )
            )
            fn.add_to_role_policy(
                iam.PolicyStatement(actions=["sqs:SendMessage"], resources=[packager_queue_arn])
            )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "CheckCommit"}},
            )
        )
        topic.grant_publish(fn)

        # -- alarms ------------------------------------------------------------
        for metric, description in (
            ("Defects", "a governed package revision carries T0 defects"),
            ("SelfApplicationFailures", "check-commit's own response revision failed verification"),
            ("EngineErrors", "the checker could not complete a run"),
        ):
            cw.Alarm(
                self,
                f"{metric}Alarm",
                alarm_description=description,
                metric=cw.Metric(
                    namespace="CheckCommit", metric_name=metric, statistic="Sum", period=Duration.minutes(5)
                ),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )

        cdk.CfnOutput(self, "FindingsTopicArn", value=topic.topic_arn)
        cdk.CfnOutput(self, "CheckerFunctionName", value=fn.function_name)
        cdk.CfnOutput(self, "EventQueueUrl", value=queue.queue_url)
