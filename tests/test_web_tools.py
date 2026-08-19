"""Web access is a deliberate, narrow grant — pin exactly who has it. No API calls."""

from jarvis.agents import BUILTIN_AGENTS
from jarvis.agents.base import ALLOWED_TOOLS, NET_TOOLS
from jarvis.orchestrator import build_orchestrator_options
import tempfile
from pathlib import Path
from jarvis.agents import RESERVED_NAMES
from jarvis.registry import AgentRegistry
from jarvis.storage import JSONStore


def test_net_tools_are_in_the_allowlist():
    assert set(NET_TOOLS) <= ALLOWED_TOOLS


def test_only_outward_facing_agents_have_web_access():
    have = {name for name, d in BUILTIN_AGENTS.items()
            if set(NET_TOOLS) & set(d.tools or [])}
    assert have == {"researcher", "internships", "startup"}


def test_offline_specialists_stay_offline():
    for name in ("homework", "leetcode", "club", "rebrand", "writer"):
        tools = set(BUILTIN_AGENTS[name].tools or [])
        assert not (set(NET_TOOLS) & tools), f"{name} should not reach the web"


def test_session_permits_the_web_tools():
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-web-tools-"))
    options = build_orchestrator_options(
        JSONStore(tmp / "plate.json"),
        AgentRegistry(tmp / "agents.json", reserved=RESERVED_NAMES),
    )
    # Without this, an accepted-edits run would hang on a permission prompt.
    assert set(NET_TOOLS) <= set(options.allowed_tools)
