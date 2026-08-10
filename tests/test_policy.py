"""Policy loading and resolution."""

import pytest

from check_commit.policy import Policy, PolicyError


def test_for_package_resolves_by_prefix():
    pol = Policy.for_package("occurrence/probability")
    assert pol.prefix == "occurrence"
    assert pol.author == "commit-protocol"
    assert pol.cast_label == "CP"
    assert pol.is_watchlisted("02-measure-selection/02-results-summary.md")
    assert not pol.is_watchlisted("code/thm_009.py")
    assert pol.adjudicated_collisions[("02-measure-selection", "21")] == frozenset({"K", "P"})


def test_unknown_prefix_is_an_error_not_a_pass():
    with pytest.raises(PolicyError):
        Policy.for_package("unknown-prefix/pkg")
