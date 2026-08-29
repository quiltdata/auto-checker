#!/usr/bin/env python3
"""Inspect and prompt Amazon Bedrock Nemotron models.

By default, ``list`` searches for Nemotron resources in us-east-1::

    python3 scripts/test_bedrock.py list
    python3 scripts/test_bedrock.py list nemotron
    python3 scripts/test_bedrock.py list nemotron --region us-west-2

``prompt`` selects the largest active on-demand Nemotron model and accepts text
as command-line arguments or, when omitted, from standard input::

    python3 scripts/test_bedrock.py prompt "Explain model distillation"
    echo "Explain model distillation" | python3 scripts/test_bedrock.py prompt
    python3 scripts/test_bedrock.py prompt --info "Explain model distillation"

Catalog operations are read-only, while ``prompt`` performs a billable model
inference. AWS credentials must allow the corresponding Bedrock list and invoke
operations.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Any, TextIO

import boto3
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_MODEL_QUERY = "nemotron"
PRICING_AS_OF = "2026-08-29"
PRICING_SOURCE = "https://aws.amazon.com/bedrock/pricing/"

# Standard on-demand USD per million input/output tokens. Bedrock's runtime
# response includes token usage but not prices, so estimates use the published
# regional rates for the model selected by this script.
ON_DEMAND_PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("nvidia.nemotron-super-3-120b", region): (0.15, 0.65)
    for region in ("us-east-1", "us-east-2", "us-west-2")
}
ON_DEMAND_PRICES.update(
    {
        ("nvidia.nemotron-super-3-120b", region): (0.18, 0.78)
        for region in (
            "us-gov-east-1",
            "us-gov-west-1",
            "ap-south-1",
            "eu-west-1",
            "eu-south-1",
            "sa-east-1",
            "ap-northeast-1",
            "ap-southeast-3",
            "eu-central-1",
            "eu-north-1",
        )
    }
)
ON_DEMAND_PRICES[("nvidia.nemotron-super-3-120b", "eu-west-2")] = (0.23, 1.01)
ON_DEMAND_PRICES[("nvidia.nemotron-super-3-120b", "ap-southeast-2")] = (0.15, 0.67)

TIER_MULTIPLIERS = {
    "default": 1.0,
    "standard": 1.0,
    "priority": 1.75,
    "flex": 0.5,
}


def list_foundation_models(client: Any) -> list[dict[str, Any]]:
    """Return all Bedrock foundation-model summaries for the current region."""
    response = client.list_foundation_models()
    return list(response.get("modelSummaries", []))


def list_inference_profiles(client: Any) -> list[dict[str, Any]]:
    """Return all Bedrock inference-profile summaries, following pagination."""
    profiles: list[dict[str, Any]] = []
    token: str | None = None

    while True:
        kwargs = {"nextToken": token} if token else {}
        response = client.list_inference_profiles(**kwargs)
        profiles.extend(response.get("inferenceProfileSummaries", []))
        token = response.get("nextToken")
        if not token:
            return profiles


def _contains(query: str, values: Iterable[Any]) -> bool:
    needle = query.casefold()
    return any(needle in str(value).casefold() for value in values if value is not None)


def matching_foundation_models(
    models: Iterable[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    """Filter and sort models by searchable Bedrock identifiers."""
    matches = [
        model
        for model in models
        if _contains(
            query,
            (
                model.get("modelId"),
                model.get("modelArn"),
                model.get("modelName"),
                model.get("providerName"),
            ),
        )
    ]
    return sorted(matches, key=lambda model: str(model.get("modelId", "")).casefold())


def matching_inference_profiles(
    profiles: Iterable[dict[str, Any]],
    query: str,
    matching_model_arns: set[str],
) -> list[dict[str, Any]]:
    """Filter and sort profiles by metadata or a matching backing model."""

    def matches(profile: dict[str, Any]) -> bool:
        model_arns = [model.get("modelArn") for model in profile.get("models", [])]
        return bool(matching_model_arns.intersection(model_arns)) or _contains(
            query,
            (
                profile.get("inferenceProfileId"),
                profile.get("inferenceProfileArn"),
                profile.get("inferenceProfileName"),
                profile.get("description"),
                *model_arns,
            ),
        )

    found = [profile for profile in profiles if matches(profile)]
    return sorted(
        found,
        key=lambda profile: str(profile.get("inferenceProfileId", "")).casefold(),
    )


def _model_size_billions(model: dict[str, Any]) -> float:
    """Extract the largest parameter count such as 120 from a ``120b`` model ID."""
    sizes = re.findall(r"(\d+(?:\.\d+)?)b\b", str(model.get("modelId", "")), re.I)
    return max((float(size) for size in sizes), default=-1.0)


def highest_available_model(
    models: Iterable[dict[str, Any]], query: str = DEFAULT_MODEL_QUERY
) -> dict[str, Any]:
    """Select the largest active, on-demand model matching ``query``."""
    candidates = [
        model
        for model in matching_foundation_models(models, query)
        if str(model.get("modelLifecycle", {}).get("status", "")).upper() == "ACTIVE"
        and "ON_DEMAND"
        in {str(value).upper() for value in model.get("inferenceTypesSupported", [])}
    ]
    if not candidates:
        raise LookupError(f"no active on-demand models matching {query!r}")

    return max(
        candidates,
        key=lambda model: (
            _model_size_billions(model),
            str(model.get("modelId", "")).casefold(),
        ),
    )


def read_prompt(arguments: Sequence[str], stdin: TextIO) -> str:
    """Read prompt text from command arguments, falling back to standard input."""
    prompt = " ".join(arguments) if arguments else stdin.read()
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt text is required as an argument or on stdin")
    return prompt


def response_text(response: dict[str, Any]) -> str:
    """Extract text blocks from a Bedrock Converse response."""
    content = response.get("output", {}).get("message", {}).get("content", [])
    text = "".join(str(block["text"]) for block in content if "text" in block).strip()
    if not text:
        raise ValueError("Bedrock response contained no text")
    return text


def estimated_cost(
    model_id: str,
    region: str,
    usage: dict[str, Any],
    service_tier: dict[str, Any],
) -> dict[str, Any]:
    """Estimate USD cost from published on-demand rates and response token usage."""
    rates = ON_DEMAND_PRICES.get((model_id, region))
    tier = str(service_tier.get("type", "default")).casefold()
    multiplier = TIER_MULTIPLIERS.get(tier)
    result: dict[str, Any] = {
        "amountUsd": None,
        "pricingAsOf": PRICING_AS_OF,
        "pricingSource": PRICING_SOURCE,
    }

    if rates is None:
        result["unavailableReason"] = (
            f"no embedded pricing for model {model_id!r} in {region!r}"
        )
        return result
    if multiplier is None:
        result["unavailableReason"] = f"unknown service tier {tier!r}"
        return result

    input_rate, output_rate = rates
    input_tokens = int(usage.get("inputTokens", 0))
    output_tokens = int(usage.get("outputTokens", 0))
    amount = (
        input_tokens * input_rate + output_tokens * output_rate
    ) / 1_000_000 * multiplier
    result.update(
        {
            "amountUsd": round(amount, 12),
            "inputRateUsdPerMillionTokens": input_rate,
            "outputRateUsdPerMillionTokens": output_rate,
            "serviceTierMultiplier": multiplier,
            "note": "estimate excludes taxes, negotiated discounts, and cache-specific pricing",
        }
    )
    return result


def runtime_info(
    response: dict[str, Any], model_id: str, region: str
) -> dict[str, Any]:
    """Build stable runtime metadata from a Bedrock Converse response."""
    usage = response.get("usage", {})
    service_tier = response.get("serviceTier", {})
    response_metadata = response.get("ResponseMetadata", {})
    info: dict[str, Any] = {
        "modelId": model_id,
        "region": region,
        "stopReason": response.get("stopReason"),
        "usage": usage,
        "metrics": response.get("metrics", {}),
        "performanceConfig": response.get("performanceConfig", {}),
        "serviceTier": service_tier,
        "estimatedCost": estimated_cost(model_id, region, usage, service_tier),
        "request": {
            "requestId": response_metadata.get("RequestId"),
            "httpStatusCode": response_metadata.get("HTTPStatusCode"),
            "retryAttempts": response_metadata.get("RetryAttempts"),
        },
    }
    if "additionalModelResponseFields" in response:
        info["additionalModelResponseFields"] = response["additionalModelResponseFields"]
    return info


def print_results(
    models: Sequence[dict[str, Any]],
    profiles: Sequence[dict[str, Any]],
    query: str,
    region: str,
) -> None:
    """Print matching resources in stable, human-readable sections."""
    print(f"Foundation models matching {query!r} in {region} ({len(models)})")
    if not models:
        print("  none")
    for model in models:
        lifecycle = model.get("modelLifecycle", {}).get("status", "UNKNOWN")
        inference_types = ",".join(model.get("inferenceTypesSupported", [])) or "unknown"
        print(
            f"  {model.get('modelId', '<unknown>')}"
            f" | provider={model.get('providerName', 'unknown')}"
            f" | lifecycle={lifecycle}"
            f" | inference={inference_types}"
        )

    print(f"\nInference profiles matching {query!r} in {region} ({len(profiles)})")
    if not profiles:
        print("  none")
    for profile in profiles:
        print(
            f"  {profile.get('inferenceProfileId', '<unknown>')}"
            f" | type={profile.get('type', 'unknown')}"
            f" | status={profile.get('status', 'UNKNOWN')}"
            f" | name={profile.get('inferenceProfileName', 'unknown')}"
        )
        for model in profile.get("models", []):
            print(f"    model: {model.get('modelArn', '<unknown>')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)

    list_parser = commands.add_parser("list", help="list matching models and profiles")
    list_parser.add_argument(
        "query",
        nargs="?",
        default=DEFAULT_MODEL_QUERY,
        help=f"case-insensitive resource search (default: {DEFAULT_MODEL_QUERY})",
    )
    list_parser.add_argument("--region", default="us-east-1")

    prompt_parser = commands.add_parser("prompt", help="prompt the largest Nemotron model")
    prompt_parser.add_argument(
        "prompt",
        nargs="*",
        help="prompt text; reads stdin when omitted",
    )
    prompt_parser.add_argument("--region", default="us-east-1")
    prompt_parser.add_argument(
        "--info",
        action="store_true",
        help="write cost, usage, latency, and request metadata as JSON to stderr",
    )
    return parser


def run_list(args: argparse.Namespace) -> int:
    client = boto3.client("bedrock", region_name=args.region)
    if not hasattr(client, "list_inference_profiles"):
        raise RuntimeError(
            "the installed boto3/botocore does not support Bedrock inference "
            "profiles; upgrade boto3"
        )

    models = matching_foundation_models(list_foundation_models(client), args.query)
    model_arns = {str(model["modelArn"]) for model in models if model.get("modelArn")}
    profiles = matching_inference_profiles(
        list_inference_profiles(client), args.query, model_arns
    )
    print_results(models, profiles, args.query, args.region)
    return 0


def run_prompt(args: argparse.Namespace, stdin: TextIO = sys.stdin) -> int:
    prompt = read_prompt(args.prompt, stdin)
    bedrock = boto3.client("bedrock", region_name=args.region)
    model = highest_available_model(list_foundation_models(bedrock))
    model_id = str(model["modelId"])

    runtime = boto3.client("bedrock-runtime", region_name=args.region)
    response = runtime.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    if args.info:
        print(
            json.dumps(runtime_info(response, model_id, args.region), indent=2, sort_keys=True),
            file=sys.stderr,
        )
    else:
        print(f"model: {model_id}", file=sys.stderr)
    print(response_text(response))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "list":
            return run_list(args)
        return run_prompt(args)
    except (BotoCoreError, ClientError, LookupError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
