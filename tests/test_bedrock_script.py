"""Hermetic tests for the standalone Bedrock Nemotron utility."""

import argparse
import importlib.util
import io
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "test_bedrock.py"


@pytest.fixture(scope="module")
def bedrock_script():
    spec = importlib.util.spec_from_file_location("bedrock_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def model(model_id, *, status="ACTIVE", inference_types=("ON_DEMAND",)):
    return {
        "modelId": model_id,
        "modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{model_id}",
        "providerName": "NVIDIA",
        "modelLifecycle": {"status": status},
        "inferenceTypesSupported": list(inference_types),
    }


class FakeControl:
    def __init__(self, models, profile_pages=()):
        self.models = models
        self.profile_pages = list(profile_pages)
        self.profile_calls = []

    def list_foundation_models(self):
        return {"modelSummaries": self.models}

    def list_inference_profiles(self, **kwargs):
        self.profile_calls.append(kwargs)
        return self.profile_pages[len(self.profile_calls) - 1]


class FakeRuntime:
    def __init__(self, response=None):
        self.response = response or {
            "output": {"message": {"content": [{"text": "answer"}]}},
            "usage": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6},
            "metrics": {"latencyMs": 5},
            "stopReason": "end_turn",
            "ResponseMetadata": {
                "RequestId": "request-id",
                "HTTPStatusCode": 200,
                "RetryAttempts": 0,
            },
        }
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def prompt_args(**overrides):
    values = {
        "prompt": [],
        "region": "us-east-1",
        "model": None,
        "max_tokens": 64,
        "info": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_dependency_floor_matches_required_bedrock_apis():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"boto3>=1.42.1,<2"' in pyproject


def test_inference_profiles_follow_next_token(bedrock_script):
    client = FakeControl(
        [],
        [
            {
                "inferenceProfileSummaries": [{"inferenceProfileId": "first"}],
                "nextToken": "page-2",
            },
            {"inferenceProfileSummaries": [{"inferenceProfileId": "second"}]},
        ],
    )

    profiles = bedrock_script.list_inference_profiles(client)

    assert [profile["inferenceProfileId"] for profile in profiles] == ["first", "second"]
    assert client.profile_calls == [{}, {"nextToken": "page-2"}]


def test_model_selection_supports_highest_and_explicit_override(bedrock_script):
    models = [
        model("nvidia.nemotron-nano-9b-v2"),
        model("nvidia.nemotron-super-3-120b"),
        model("nvidia.nemotron-ultra-500b", status="LEGACY"),
        model("nvidia.nemotron-provisioned-300b", inference_types=("PROVISIONED",)),
    ]

    assert bedrock_script.select_model(models)["modelId"] == "nvidia.nemotron-super-3-120b"
    assert (
        bedrock_script.select_model(models, "nvidia.nemotron-nano-9b-v2")["modelId"]
        == "nvidia.nemotron-nano-9b-v2"
    )
    with pytest.raises(LookupError, match="not an active on-demand Nemotron"):
        bedrock_script.select_model(models, "nvidia.nemotron-ultra-500b")


def test_missing_sdk_method_has_upgrade_guidance(bedrock_script):
    with pytest.raises(RuntimeError, match=r"install boto3>=1\.42\.1"):
        bedrock_script.list_foundation_models(object())
    with pytest.raises(RuntimeError, match=r"BedrockRuntime\.converse"):
        bedrock_script.require_client_method(object(), "converse", "BedrockRuntime")


def test_max_tokens_must_be_positive(bedrock_script):
    parser = bedrock_script.build_parser()
    assert parser.parse_args(["prompt", "--max-tokens", "1", "hi"]).max_tokens == 1
    with pytest.raises(SystemExit):
        parser.parse_args(["prompt", "--max-tokens", "0", "hi"])


def test_prompt_from_stdin_passes_cap_and_reports_info(
    bedrock_script, monkeypatch, capsys
):
    control = FakeControl([model("nvidia.nemotron-super-3-120b")])
    runtime = FakeRuntime()

    def client(service, region_name):
        assert region_name == "us-east-1"
        return runtime if service == "bedrock-runtime" else control

    monkeypatch.setattr(bedrock_script.boto3, "client", client)

    assert (
        bedrock_script.run_prompt(
            prompt_args(info=True), io.StringIO("prompt from stdin\n")
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == "answer\n"
    info = json.loads(captured.err)
    assert info["modelId"] == "nvidia.nemotron-super-3-120b"
    assert info["invocation"] == {"maxTokens": 64}
    assert info["estimatedCost"]["amountUsd"] == 1.9e-06
    assert runtime.calls == [
        {
            "modelId": "nvidia.nemotron-super-3-120b",
            "messages": [
                {"role": "user", "content": [{"text": "prompt from stdin"}]}
            ],
            "inferenceConfig": {"maxTokens": 64},
        }
    ]


def test_explicit_unpriced_model_is_allowed_without_info(
    bedrock_script, monkeypatch, capsys
):
    model_id = "nvidia.nemotron-nano-3-30b"
    control = FakeControl([model(model_id), model("nvidia.nemotron-super-3-120b")])
    runtime = FakeRuntime()
    monkeypatch.setattr(
        bedrock_script.boto3,
        "client",
        lambda service, region_name: runtime if service == "bedrock-runtime" else control,
    )

    assert (
        bedrock_script.run_prompt(
            prompt_args(prompt=["hello"], model=model_id), io.StringIO()
        )
        == 0
    )

    captured = capsys.readouterr()
    assert captured.out == "answer\n"
    assert f"model: {model_id}" in captured.err
    assert runtime.calls[0]["modelId"] == model_id


def test_automatic_unpriced_model_fails_before_invocation(bedrock_script, monkeypatch):
    control = FakeControl(
        [
            model("nvidia.nemotron-super-3-120b"),
            model("nvidia.nemotron-future-999b"),
        ]
    )

    def client(service, region_name):
        if service == "bedrock-runtime":
            pytest.fail("runtime client must not be created for an unpriced auto-selection")
        return control

    monkeypatch.setattr(bedrock_script.boto3, "client", client)

    with pytest.raises(RuntimeError, match="no embedded pricing"):
        bedrock_script.run_prompt(prompt_args(prompt=["hello"]), io.StringIO())


def test_info_requires_pricing_for_explicit_model(bedrock_script, monkeypatch):
    model_id = "nvidia.nemotron-nano-3-30b"
    control = FakeControl([model(model_id)])
    monkeypatch.setattr(bedrock_script.boto3, "client", lambda service, region_name: control)

    with pytest.raises(RuntimeError, match="no embedded pricing"):
        bedrock_script.run_prompt(
            prompt_args(prompt=["hello"], model=model_id, info=True), io.StringIO()
        )
