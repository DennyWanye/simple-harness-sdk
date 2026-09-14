"""Explicit verification dispatch. Unknown domains never inherit document semantics."""

from __future__ import annotations

from typing import Any

from ..contracts import ContractError
from ..governance.domains import APPWORLD_DOMAIN, CODE_DOMAIN, DOC_DOMAIN, DomainProfileV1
from .deterministic_checks import LayerResult, rule_check


class CodeHandler:
    name = "code"
    document_assessments = False

    def rules(self, *args: Any, **kwargs: Any) -> LayerResult:
        return rule_check(*args, **kwargs)


class DocumentHandler(CodeHandler):
    name = "document"
    document_assessments = True


class AppWorldHandler(CodeHandler):
    name = "appworld"

    def rules(self, *args: Any, **kwargs: Any) -> LayerResult:
        # This gate checks submitted reports and references only. Official task
        # success is scored from the saved world after ALL agent activity stops.
        kwargs["require_synthesis_knowledge"] = False
        kwargs["local_code_execution"] = False
        result = rule_check(*args, **kwargs)
        return LayerResult(
            result.layer,
            result.status,
            result.summary,
            {
                **dict(result.detail),
                "handler": self.name,
                "benchmark_success": "external_evaluation_pending",
            },
        )


_HANDLERS = {
    CODE_DOMAIN: CodeHandler(),
    DOC_DOMAIN: DocumentHandler(),
    APPWORLD_DOMAIN: AppWorldHandler(),
}


def handler_for(domain: DomainProfileV1 | None) -> CodeHandler:
    domain_id = CODE_DOMAIN if domain is None else domain.id
    try:
        return _HANDLERS[domain_id]
    except KeyError as error:
        raise ContractError(f"No verification handler for domain {domain_id}") from error
