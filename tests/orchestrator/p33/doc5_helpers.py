"""Explicit current-document fixture policy, without changing the shared code helpers."""

from graph_helpers7 import DIAMOND
from graph_helpers7 import graph_service as legacy_graph_service
from graph_helpers7 import node as legacy_node

from agent_orchestrator.governance.domains import DOC_DOMAIN

REVIEWED = ["format_check", "rule_check", "critic_review"]


def node(*args, **kwargs):
    kwargs.setdefault("verification_policy", REVIEWED)
    return legacy_node(*args, **kwargs)


def graph_service(path, **kwargs):
    if kwargs.get("domain") == DOC_DOMAIN and kwargs.get("nodes") is None:
        kwargs["nodes"] = [{**item, "verification_policy": REVIEWED} for item in DIAMOND]
    return legacy_graph_service(path, **kwargs)
