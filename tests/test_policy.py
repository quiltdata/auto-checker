"""Policy loading, regime selection, and the protocol forms."""

import pytest

from check_commit import policy as forms
from check_commit.policy import CURRENT, PRE_MIGRATION, Policy, PolicyError


def test_for_package_resolves_by_prefix():
    pol = Policy.for_package("occurrence/probability")
    assert pol.prefix == "occurrence"
    assert pol.author == "commit-protocol"
    assert pol.regime == CURRENT
    assert pol.workflow == "occurrence"
    assert pol.contributor == "Checker"


def test_unknown_prefix_is_an_error_not_a_pass():
    with pytest.raises(PolicyError):
        Policy.for_package("unknown-prefix/pkg")


def test_watchlist_is_regime_scoped():
    """The retired watchlist names message folders that no longer exist; the
    live one names the prefix's normative surface."""
    pol = Policy.for_package("occurrence/spec")
    assert pol.is_watchlisted("protocol/occurrence.md")
    assert not pol.is_watchlisted("02-measure-selection/02-results-summary.md")
    assert pol.is_watchlisted("02-measure-selection/02-results-summary.md", PRE_MIGRATION)
    assert not pol.is_watchlisted("protocol/occurrence.md", PRE_MIGRATION)


def test_retired_tunables_are_not_live_config():
    """`structured_file_fields` and the collision adjudications survive only
    under `pre_migration`, where no current-regime check can reach them."""
    pol = Policy.for_package("occurrence/spec")
    assert not hasattr(pol, "structured_file_fields")
    assert not hasattr(pol, "cast_label")
    assert pol.legacy.structured_file_fields["messages_added"] == "added"
    assert pol.legacy.adjudicated_collisions[("02-measure-selection", "21")] == frozenset({"K", "P"})


def test_vendored_schema_matches_the_registered_one():
    """The vendored copy is the artifact §2 makes a defect when stale, so it
    must at least parse and declare the registered contract."""
    import json

    pol = Policy.for_package("occurrence/spec")
    schema = json.loads(pol.vendored_schema_path.read_text())
    assert schema["$id"] == "quilt-occurrence"
    assert schema["required"] == ["related_packages", "status"]
    assert schema["additionalProperties"] is False
    assert "^issues/[^/]+$" in schema["patternProperties"]


def test_unknown_regime_refuses_to_load(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text("author: a\nregime: sideways\nworkflow: w\nwatchlist: []\n")
    with pytest.raises(PolicyError):
        Policy.load(p)


# --- protocol forms ---------------------------------------------------------

def test_issue_folder_and_turn_forms():
    assert forms.issue_folder_of("issues/053-header-placement/053.01-Comments-x.md") == (
        "issues/053-header-placement"
    )
    assert forms.issue_folder_of("issues/closed/041-gate.md") is None
    assert forms.issue_number("issues/053-header-placement") == "053"
    assert forms.ISSUE_README_RE.match("issues/052-create/README.md")

    assert forms.turn_parts("053.01-Comments-header-after-h1-proposal.md") == (
        "053",
        "01",
        "Comments",
        "header-after-h1-proposal",
    )
    assert forms.turn_parts("00.01-Klein-creation-audit.md")[2] == "Klein"
    assert forms.turn_parts("066.03-HC-vetting-global-cyclic-incidence-geometry.md")[2] == "HC"
    assert forms.turn_parts("README.md") is None
    # legacy numeric turns still hold a number, so they still count for ordering
    assert forms.turn_parts("001.md") is None
    assert forms.turn_number("001.md") == 1
    assert forms.turn_number("053.07-PM-x.md") == 7


def test_route_key_namespace_matches_the_schema():
    """`^issues/[^/]+$` — folder-path keys and legacy flat `.md` keys, nothing
    deeper."""
    assert forms.ROUTE_KEY_RE.match("issues/051-decouple-information-scope-from-authority")
    assert forms.ROUTE_KEY_RE.match("issues/047-packet-fence-review.md")
    assert not forms.ROUTE_KEY_RE.match("issues/051-decouple/README.md")
    assert not forms.ROUTE_KEY_RE.match("related_packages")


def test_own_turn_recognition():
    """The checker's replacement for the retired `author` metadata field."""
    pol = Policy.for_package("occurrence/spec")
    assert pol.is_own_turn("issues/053-x/053.02-Checker-t0-check-of-abcd1234.md")
    assert not pol.is_own_turn("issues/053-x/053.02-Owner-disposition.md")
    assert not pol.is_own_turn("issues/053-x/053.02-Checker-unrelated-work.md")
    assert not pol.is_own_turn("issues/053-x/README.md")
    assert pol.response_slug("abcd1234" + "0" * 56) == "t0-check-of-abcd1234"
