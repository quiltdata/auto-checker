"""Write-back's one registry call: a package patch (quilt-specs#39).

A patch applies the logical keys it names to a parent revision and nothing
else, so the response revision is the parent plus one turn no matter what else
sits under the package's backing prefix.
"""

from __future__ import annotations

import json
import urllib.request

# Synchronous packagePatch, not packagePatchAsync: a queued patch that fails is
# reported to no one and not retried, so the caller could not resubmit it.
PATCH = """
mutation ($params: PackagePushParams!, $src: PackagePatchSource!) {
  packagePatch(params: $params, src: $src) {
    __typename
    ... on PackagePushSuccess { revision { hash } }
    ... on InvalidInput { errors { path name message } }
    ... on OperationError { name message context }
  }
}
"""


class PatchError(Exception):
    """The registry did not apply the patch."""


class ParentNotLatest(PatchError):
    """Another revision became latest after the one the patch was pinned to."""

    def __init__(self, message: str, latest: str | None):
        super().__init__(message)
        self.latest = latest


class Registry:
    def __init__(self, url: str, api_key: str, timeout: float = 60):
        self.endpoint = f"{url.rstrip('/')}/graphql"
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_secret(cls, url: str, secret_id: str, region: str | None = None) -> "Registry":
        """The API key is the secret's whole string value."""
        import boto3

        secret = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_id)
        return cls(url, secret["SecretString"].strip())

    def patch(
        self, *, bucket: str, name: str, parent: str, entries: dict[str, str], message: str, workflow: str
    ) -> str:
        """Set `entries` (logical key -> physical key) on `parent`; return the new top hash.

        Pinned: if `parent` is no longer latest, the registry writes nothing and
        this raises ParentNotLatest.
        """
        variables = {
            # No userMeta: omitted keeps the parent's package metadata.
            "params": {"bucket": bucket, "name": name, "message": message, "workflow": workflow},
            "src": {
                "parent": parent,
                "set": [{"logicalKey": lk, "physicalKey": pk} for lk, pk in entries.items()],
            },
        }
        result = self._post(PATCH, variables)["packagePatch"]
        kind = result["__typename"]
        if kind == "PackagePushSuccess":
            return result["revision"]["hash"]
        if kind == "OperationError" and result["name"] == "ParentNotLatest":
            raise ParentNotLatest(result["message"], (result.get("context") or {}).get("latest"))
        if kind == "OperationError":
            raise PatchError(f"{result['name']}: {result['message']}")
        if kind == "InvalidInput":
            raise PatchError("; ".join(f"{e['name']} at {e['path']}: {e['message']}" for e in result["errors"]))
        raise PatchError(f"unexpected result {kind}")

    def _post(self, query: str, variables: dict) -> dict:
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.load(response)
        if body.get("errors"):
            raise PatchError("; ".join(e.get("message", str(e)) for e in body["errors"]))
        return body["data"]
