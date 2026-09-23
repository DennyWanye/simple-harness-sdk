"""Portable paths for opt-in diagnostic seams; output always stays Git-ignored."""
from datetime import datetime
from pathlib import Path
import sys

SDK = Path(__file__).resolve().parents[2]
EVIDENCE = SDK.parents[1] / ".local-test-evidence" / datetime.now().strftime("%Y-%m-%d") / "assurance-integration"
EVIDENCE.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(SDK / "src"), str(SDK / "tests/agents"),
               str(SDK / "tests/orchestrator/full_target"),
               str(SDK / "tests/orchestrator/full_target/operation_completion")]


def seam_tool_ports(root, artifact_store=None):
    """Real original ToolGateway over an empty WorkspaceManager, as the assured review
    template now lists the two read-only evidence tools. Returns (gateway, ports kwargs)."""
    from agent_orchestrator.artifacts.workspace import WorkspaceManager
    from agent_orchestrator.runtime.tool_gateway import TOOL_NAMES, WorkspaceToolGateway, read_tool_schemas

    gateway = WorkspaceToolGateway(
        WorkspaceManager(Path(root) / "seam-workspaces", artifact_store=artifact_store))
    return gateway, {"tool_executor": gateway, "tool_names": TOOL_NAMES, "tool_schemas": read_tool_schemas()}
