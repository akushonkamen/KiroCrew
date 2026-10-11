"""Whether an agent-written dashboard page may run script, decided by the ceiling.

The RFC amendment licenses a running owner-written page on four reasons, and the
fourth is that the owner agent already reaches the network, so a page it wrote
navigating its own frame opens no path that agent did not already have.

That reason holds only while the agent's egress is UNRESTRICTED. A host allowlist
is the case worth spelling out: the agent may fetch one host and nothing else,
but a frame that runs script can navigate ITSELF anywhere, so the page would
reach hosts the ceiling never permitted. "Some egress" is not the premise.

So scripts run only for an agent nothing restricts, and the three cases below
are the ones that decide it: unrestricted runs, an allowlist does not, deny-all
does not. The page is then static and its fields still fill, because a
`data-dashboard-field` binding is the HOST writing `textContent` into the
composed document and needs no script inside the frame.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.platform import governance as gov


def _ruleset(mode: str, allow: tuple[str, ...] = (), deny: tuple[str, ...] = ()) -> Any:
    return gov.ScopedRuleset(mode=mode, allow=allow, deny=deny, matcher="host")


def _ceiling_with(control: Any, *, governed: bool = True) -> Any:
    controls = {"network.egress": control} if governed else {}
    return gov.GovernanceCeiling(version=1, boot=gov.BootControls(), controls=controls)


@pytest.fixture
def ceiling(monkeypatch):
    def _install(control: Any, *, governed: bool = True) -> None:
        monkeypatch.setattr(
            gov, "last_composed_ceiling", lambda: _ceiling_with(control, governed=governed)
        )

    return _install


class TestTheThreeCasesThatDecideIt:
    """The ruling's own three cases, each asserted on its own."""

    def test_an_unrestricted_agent_gets_scripts(self, ceiling) -> None:
        """A deny-mode ruleset that denies NOTHING restricts nothing."""
        ceiling(_ruleset(gov.MODE_DENY, deny=()))
        assert md.owner_agent_egress_unrestricted() is True

    def test_a_host_allowlist_gets_NO_scripts(self, ceiling) -> None:
        """THE HOLE THIS CLOSES. The agent may reach `api.github.com` and nothing
        else, so a page that can navigate its own frame would reach hosts the
        ceiling never permitted. An allow-mode ruleset is a closed set, so it
        restricts even though it names a host the agent may use."""
        ceiling(_ruleset(gov.MODE_ALLOW, allow=("api.github.com",)))
        assert md.owner_agent_egress_unrestricted() is False

    def test_deny_all_gets_NO_scripts(self, ceiling) -> None:
        ceiling(_ruleset(gov.MODE_DENY, deny=("*",)))
        assert md.owner_agent_egress_unrestricted() is False


class TestTheUngovernedDefaults:
    """Every install that configures no policy is unchanged."""

    def test_no_ceiling_gets_scripts(self, monkeypatch) -> None:
        monkeypatch.setattr(gov, "last_composed_ceiling", lambda: None)
        assert md.owner_agent_egress_unrestricted() is True

    def test_a_scope_the_ceiling_does_not_govern_gets_scripts(self, ceiling) -> None:
        ceiling(None, governed=False)
        assert md.owner_agent_egress_unrestricted() is True


class TestItFailsClosed:
    """A page runs only when the ceiling was READ and found to restrict nothing."""

    @pytest.mark.parametrize("pattern", ["*", "**", "evil.example", "*.internal"])
    def test_any_deny_pattern_withholds_scripts(self, ceiling, pattern: str) -> None:
        """One host denied is still a restriction: the premise is "no
        restriction", not "the agent can still reach something"."""
        ceiling(_ruleset(gov.MODE_DENY, deny=(pattern,)))
        assert md.owner_agent_egress_unrestricted() is False

    def test_an_empty_allowlist_withholds_scripts(self, ceiling) -> None:
        ceiling(_ruleset(gov.MODE_ALLOW, allow=()))
        assert md.owner_agent_egress_unrestricted() is False

    def test_an_archetype_this_handler_cannot_read_withholds_scripts(self, ceiling) -> None:
        """Something governs the scope and this code does not know its shape.
        Guessing "unrestricted" would hand out script on a ceiling nobody here
        understood."""
        ceiling(object())
        assert md.owner_agent_egress_unrestricted() is False

    def test_an_exception_withholds_scripts(self, monkeypatch) -> None:
        """The direction that cannot open an egress path."""

        def _boom() -> Any:
            raise RuntimeError("the ceiling could not be read")

        monkeypatch.setattr(gov, "last_composed_ceiling", _boom)
        assert md.owner_agent_egress_unrestricted() is False
