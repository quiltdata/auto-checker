#!/usr/bin/env python3
"""Manage subscriptions on the check-commit findings topic (04 §9).

The topic ARN is discovered from the deployed stack's FindingsTopicArn
output, so the default invocation needs no arguments beyond the action:

    python3 scripts/sns.py subscribe --email you@example.com
    python3 scripts/sns.py list
    python3 scripts/sns.py unsubscribe <subscription-arn>
"""

from __future__ import annotations

import argparse
import sys

import boto3


def topic_arn(stack_name: str, region: str) -> str:
    cfn = boto3.client("cloudformation", region_name=region)
    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    for output in stacks[0].get("Outputs", []):
        if output["OutputKey"] == "FindingsTopicArn":
            return output["OutputValue"]
    raise SystemExit(f"error: no FindingsTopicArn output on stack {stack_name!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["subscribe", "list", "unsubscribe"])
    ap.add_argument("subscription_arn", nargs="?", help="for unsubscribe")
    ap.add_argument("--email", help="email endpoint (subscribe)")
    ap.add_argument("--sms", help="SMS endpoint (subscribe)")
    ap.add_argument("--url", help="HTTPS endpoint (subscribe)")
    ap.add_argument("--stack-name", default="check-commit")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    sns = boto3.client("sns", region_name=args.region)
    arn = topic_arn(args.stack_name, args.region)

    if args.action == "subscribe":
        endpoints = [("email", args.email), ("sms", args.sms), ("https", args.url)]
        chosen = [(proto, ep) for proto, ep in endpoints if ep]
        if len(chosen) != 1:
            print("error: pass exactly one of --email/--sms/--url", file=sys.stderr)
            return 2
        proto, endpoint = chosen[0]
        resp = sns.subscribe(TopicArn=arn, Protocol=proto, Endpoint=endpoint, ReturnSubscriptionArn=True)
        print(f"subscribed ({proto}): {resp['SubscriptionArn']}")
        if proto == "email":
            print("check your inbox to confirm the subscription")
    elif args.action == "list":
        subs = sns.list_subscriptions_by_topic(TopicArn=arn)["Subscriptions"]
        if not subs:
            print(f"no subscriptions on {arn}")
        for s in subs:
            print(f"{s['Protocol']:<8} {s['Endpoint']:<40} {s['SubscriptionArn']}")
    else:
        if not args.subscription_arn:
            print("error: unsubscribe needs the subscription ARN (see `list`)", file=sys.stderr)
            return 2
        sns.unsubscribe(SubscriptionArn=args.subscription_arn)
        print("unsubscribed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
