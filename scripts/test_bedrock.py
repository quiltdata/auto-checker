#!/usr/bin/env python3
"""Inspect Amazon Bedrock foundation models and inference profiles.

By default, ``list`` searches for Nemotron resources in us-east-1::

    python3 scripts/test_bedrock.py list
    python3 scripts/test_bedrock.py list nemotron
    python3 scripts/test_bedrock.py list nemotron --region us-west-2

The command is read-only. It requires AWS credentials with permission to call
``bedrock:ListFoundationModels`` and ``bedrock:ListInferenceProfiles``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError


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
    parser.add_argument("action", choices=["list"])
    parser.add_argument(
        "query",
        nargs="?",
        default="nemotron",
        help="case-insensitive resource search (default: nemotron)",
    )
    parser.add_argument("--region", default="us-east-1")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = boto3.client("bedrock", region_name=args.region)

    if not hasattr(client, "list_inference_profiles"):
        print(
            "error: the installed boto3/botocore does not support Bedrock "
            "inference profiles; upgrade boto3",
            file=sys.stderr,
        )
        return 2

    try:
        models = matching_foundation_models(list_foundation_models(client), args.query)
        model_arns = {
            str(model["modelArn"]) for model in models if model.get("modelArn")
        }
        profiles = matching_inference_profiles(
            list_inference_profiles(client), args.query, model_arns
        )
    except (BotoCoreError, ClientError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print_results(models, profiles, args.query, args.region)
    return 0


if __name__ == "__main__":
    sys.exit(main())
