"""Template assertions for the check-commit stack.

These cover the failure classes that actually bit this stack, none of which the
unit suite or a plain `cdk synth` can see:

  - A queue visibility timeout below the function timeout. Synth emits a valid
    template and Lambda rejects the event source mapping at deploy, so the only
    cheap place to catch it is here, against the synthesized template.
  - Write-path grants and the Packager queue import leaking into a notify-only
    deployment, where the Lambda has no use for either.

Assertions are on relations and on the presence or absence of grants, not on a
snapshot of the whole template: a snapshot of infrastructure mostly generates
churn on unrelated CDK upgrades.

Needs `aws-cdk-lib` (cdk/requirements.txt) and a built Lambda asset
(`scripts/build-lambda.sh`), neither of which the unit job installs. The `cdk`
CI job provides both and checks for them before running, so a skip here cannot
pass for a green run there.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

CDK_DIR = pathlib.Path(__file__).resolve().parent.parent / "cdk"

pytest.importorskip("aws_cdk", reason="aws-cdk-lib not installed (see cdk/requirements.txt)")

if str(CDK_DIR) not in sys.path:
    # cdk/ is a directory of scripts rather than a package; app.py imports
    # `stack` the same way.
    sys.path.insert(0, str(CDK_DIR))

from aws_cdk import App  # noqa: E402
from aws_cdk.assertions import Template  # noqa: E402

import app as app_mod  # noqa: E402
import stack as stack_mod  # noqa: E402

pytestmark = pytest.mark.skipif(
    not stack_mod.LAMBDA_ASSET.is_dir(),
    reason=f"Lambda asset missing at {stack_mod.LAMBDA_ASSET}; run scripts/build-lambda.sh",
)


CDK_JSON = CDK_DIR / "cdk.json"


def checked_in_context() -> dict:
    """The context `cdk deploy` actually uses, from cdk.json."""
    return json.loads(CDK_JSON.read_text())["context"]


def synth(**overrides) -> Template:
    """Synthesize with the checked-in context, as the CDK CLI would.

    `App()` does not read cdk.json — the CLI does, and passes it in. Constructing
    `App` with no context would assert the fallbacks in stack.py rather than the
    deployment configuration, so a cdk.json that enabled write-back against a
    governed registry would leave every notify-only assertion below still
    passing. Overrides win, mirroring `--context` on the command line.
    """
    context = {**checked_in_context(), **overrides}
    return Template.from_stack(stack_mod.CheckCommitStack(App(context=context), "check-commit"))


def as_list(value) -> list:
    """CloudFormation collapses a single-element Action/Resource list to a string."""
    return [value] if isinstance(value, str) else value


def only(resources: dict, predicate) -> dict:
    """The single resource matching predicate, so a miscount fails loudly."""
    hits = [r for r in resources.values() if predicate(r)]
    assert len(hits) == 1, f"expected exactly one match, got {len(hits)}"
    return hits[0]


def event_queue(template: Template) -> dict:
    queues = template.find_resources("AWS::SQS::Queue")
    assert len(queues) == 2, "expected the event queue and its DLQ"
    # The event queue is the one with a redrive policy; the DLQ is the target.
    return only(queues, lambda r: "RedrivePolicy" in r["Properties"])


def checker(template: Template) -> dict:
    fns = template.find_resources("AWS::Lambda::Function")
    return only(fns, lambda r: r["Properties"].get("Handler", "").startswith("check_commit"))


def policy_actions(template: Template) -> list[str]:
    """Every action granted to the checker's role, flattened."""
    actions = []
    for pol in template.find_resources("AWS::IAM::Policy").values():
        for statement in pol["Properties"]["PolicyDocument"]["Statement"]:
            actions.extend(as_list(statement.get("Action", [])))
    return actions


# -- the checked-in deployment target --------------------------------------


def test_cdk_json_targets_the_open_account_notify_only():
    """cdk.json is what a bare `cdk deploy` uses, so it is worth asserting.

    Retargeting the corpus is the point of this configuration; changing it should
    be a deliberate edit to this test, not a silent one to cdk.json.
    """
    assert checked_in_context() == {
        "account": "867344438354",
        "region": "us-east-1",
        "quiltStackName": "open-quilt-bio",
        "packagePrefix": "occurrence",
        "registryBuckets": "protology",
        "writeBack": "false",
    }


def test_the_app_targets_that_account_and_region():
    """Asserted through the real app, not the stack in isolation.

    The stack does not read `account` or `region`; `app.py` turns them into
    `env=cdk.Environment(...)`. Asserting only cdk.json's contents would leave
    that wiring uncovered, and removing it makes the stack environment-agnostic —
    it would deploy to whichever account the ambient credentials name, which is
    the failure pinning `account` exists to prevent.
    """
    assembly = app_mod.build_app(context=checked_in_context()).synth()
    env = assembly.get_stack_by_name("check-commit").environment
    assert (env.account, env.region) == ("867344438354", "us-east-1")


def test_the_app_is_not_environment_agnostic():
    """An unknown account is what "deploys wherever the credentials point" looks
    like in the synthesized assembly, so name it rather than trust the test above
    to notice."""
    assembly = app_mod.build_app(context=checked_in_context()).synth()
    env = assembly.get_stack_by_name("check-commit").environment
    assert "unknown-account" not in env.account
    assert "unknown-region" not in env.region


def test_stack_fallbacks_agree_with_the_checked_in_context():
    """The `or "..."` defaults in stack.py and cdk.json must not drift apart.

    Both are reachable — cdk.json for a normal deploy, the fallbacks when a
    caller synthesizes the stack without it — so disagreeing would mean two
    different deployments depending on how the app was invoked.
    """
    context = checked_in_context()
    bare = Template.from_stack(stack_mod.CheckCommitStack(App(), "check-commit"))
    with_json = synth()
    assert bare.to_json() == with_json.to_json(), (
        "stack.py's fallback defaults produce a different template than "
        f"cdk.json's context {context}"
    )


# -- the bug that broke the first deploy -----------------------------------


def test_visibility_timeout_is_not_below_the_function_timeout():
    """Lambda refuses the event source mapping otherwise.

    This is the hard service constraint, and it is what failed the initial
    create in the open account. Asserted as a relation so it holds however the
    two values are chosen.
    """
    template = synth()
    visibility = event_queue(template)["Properties"]["VisibilityTimeout"]
    timeout = checker(template)["Properties"]["Timeout"]
    assert visibility >= timeout, (
        f"queue visibility {visibility}s is below the function timeout {timeout}s; "
        "Lambda rejects the event source mapping"
    )


def test_visibility_timeout_follows_the_retry_guidance():
    """Six times the function timeout, per AWS's SQS/Lambda retry guidance.

    Clearing the minimum above is not enough. With reserved concurrency of one,
    a backlog leaves messages received-but-throttled; visibility expiring while
    they wait redelivers them and climbs their receive count toward
    max_receive_count on throttling alone, dead-lettering sound events.
    """
    template = synth()
    visibility = event_queue(template)["Properties"]["VisibilityTimeout"]
    timeout = checker(template)["Properties"]["Timeout"]
    assert visibility == timeout * stack_mod.VISIBILITY_RETRY_FACTOR


def test_checker_runs_one_revision_at_a_time():
    """Serializes checks and the response counter allocation (04 §6)."""
    assert checker(synth())["Properties"]["ReservedConcurrentExecutions"] == 1


def test_event_queue_dead_letters_after_three_receives():
    redrive = event_queue(synth())["Properties"]["RedrivePolicy"]
    assert redrive["maxReceiveCount"] == 3


# -- notify-only holds no write path ---------------------------------------


def test_notify_only_is_the_default():
    assert checker(synth())["Properties"]["Environment"]["Variables"]["WRITE_BACK"] == "false"


def test_notify_only_grants_no_write_access():
    """No PutObject on the governed prefix, no SendMessage to the Packager.

    The Lambda cannot use either with write-back off, and this registry
    validates every write.
    """
    actions = policy_actions(synth())
    assert "s3:PutObject" not in actions
    # sqs:SendMessage appears in the EventBridge -> queue policy, which is a
    # resource policy rather than a grant to the checker; the role must not have
    # it. find_resources("AWS::IAM::Policy") only covers role policies.
    assert "sqs:SendMessage" not in actions


def test_notify_only_does_not_import_the_packager_queue():
    """A missing export is a deploy-time failure, so importing it unconditionally
    would break a notify-only deployment against a Quilt stack that has none."""
    body = json.dumps(synth().to_json())
    assert "Fn::ImportValue" not in body


def test_notify_only_leaves_the_packager_queue_url_empty():
    env = checker(synth())["Properties"]["Environment"]["Variables"]
    assert env["PACKAGER_QUEUE_URL"] == ""


# -- write-back wires the write path back up -------------------------------


def test_write_back_imports_both_packager_exports():
    body = json.dumps(synth(writeBack="true", quiltStackName="some-quilt").to_json())
    assert "some-quilt-PackagerQueueArn" in body
    assert "some-quilt-PackagerQueueUrl" in body


def test_write_back_grants_the_write_path():
    actions = policy_actions(synth(writeBack="true"))
    assert "s3:PutObject" in actions
    assert "sqs:SendMessage" in actions


# -- permissions stay scoped to the governed prefix (04 §7) ----------------


def test_object_reads_are_scoped_to_the_prefix_and_dot_quilt():
    """Object reads reach the governed prefix and .quilt, and nothing else.

    Scoped to GetObject/GetObjectVersion. Listing is broader; see the test below.
    """
    template = synth(registryBuckets="reg-one", packagePrefix="myprefix")
    resources = []
    for pol in template.find_resources("AWS::IAM::Policy").values():
        for statement in pol["Properties"]["PolicyDocument"]["Statement"]:
            if "s3:GetObject" in as_list(statement.get("Action", [])):
                resources.extend(as_list(statement["Resource"]))
    assert sorted(resources) == [
        "arn:aws:s3:::reg-one/.quilt/*",
        "arn:aws:s3:::reg-one/myprefix/*",
    ]


def test_listing_is_bucket_wide_and_that_is_recorded_not_asserted_away():
    """`s3:ListBucket` carries no `s3:prefix` condition, so the checker can
    enumerate every key in the registry bucket, not just the governed prefix.

    Object reads are scoped; listing is not. This test states the gap rather than
    letting the scoping test above imply it does not exist. Narrowing it means
    adding an `s3:prefix` condition covering `{prefix}/*` and `.quilt/*`, which
    needs deploy-time confirmation that quilt3's own listing still succeeds —
    tracked separately rather than changed blind against a live registry.
    """
    template = synth(registryBuckets="reg-one", packagePrefix="myprefix")
    for pol in template.find_resources("AWS::IAM::Policy").values():
        for statement in pol["Properties"]["PolicyDocument"]["Statement"]:
            if "s3:ListBucket" in as_list(statement.get("Action", [])):
                assert as_list(statement["Resource"]) == ["arn:aws:s3:::reg-one"]
                assert "Condition" not in statement, (
                    "a condition appeared on ListBucket: if the prefix scoping "
                    "was tightened, assert the allowed prefixes here instead"
                )
                return
    pytest.fail("no s3:ListBucket statement found")


def test_write_grant_is_scoped_to_the_prefix():
    template = synth(writeBack="true", registryBuckets="reg-one", packagePrefix="myprefix")
    for pol in template.find_resources("AWS::IAM::Policy").values():
        for statement in pol["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Sid") == "WriteOwnMessagesOnly":
                assert as_list(statement["Resource"]) == ["arn:aws:s3:::reg-one/myprefix/*"]
                return
    pytest.fail("no WriteOwnMessagesOnly statement found with write-back enabled")


def test_every_registry_bucket_is_granted():
    template = synth(registryBuckets="reg-one,reg-two")
    body = json.dumps(template.to_json())
    assert "arn:aws:s3:::reg-one" in body
    assert "arn:aws:s3:::reg-two" in body


# -- ingress matches the governed prefix and nothing else ------------------


def test_rule_matches_quilt_package_revisions_under_the_prefix():
    rules = synth(packagePrefix="myprefix").find_resources("AWS::Events::Rule")
    pattern = only(rules, lambda r: "EventPattern" in r["Properties"])["Properties"][
        "EventPattern"
    ]
    assert pattern["source"] == ["com.quiltdata"]
    assert pattern["detail-type"] == ["package-revision"]
    assert pattern["detail"]["handle"] == [{"prefix": "myprefix/"}]


def test_policy_prefix_reaches_the_lambda():
    """The prefix selects the policy file at runtime, so it has to be passed."""
    env = checker(synth(packagePrefix="myprefix"))["Properties"]["Environment"]["Variables"]
    assert env["PACKAGE_PREFIX"] == "myprefix"


# -- alarms exist to be subscribed ----------------------------------------


def test_the_three_alarms_are_present():
    alarms = synth().find_resources("AWS::CloudWatch::Alarm")
    names = {a["Properties"]["MetricName"] for a in alarms.values()}
    assert names == {"Defects", "SelfApplicationFailures", "EngineErrors"}
