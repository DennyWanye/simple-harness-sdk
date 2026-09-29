# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""架构方案 C（2026-09-30）：回答规划问题时，把回答里带的资料登记成任务资料。

用户回答"缺什么资料"的问题时给出的内容，只写成任务级备注的话执行者拿不到文件。资料是
按每次尝试冻结、只读挂载的（域的 ``source_roots``），所以把回答登记为
``<资料根>answers/<问题 id>.md``：规划器让那一步原样重试，新尝试自动挂上这份文件。
不加决定种类、不改规划包、不改模板。回答与登记在同一事务里，两者都按问题 id 幂等。
"""

from __future__ import annotations

from typing import Any

from ..contracts import ContractError
from ..storage.planning_human_store import PlanningHumanStore

ANSWER_SOURCE_KIND = "markdown"


def answer_source_path(source_root: str, decision_id: str) -> str:
    return f"{source_root}answers/{decision_id}.md"


def answer_planning_question(
    orchestrator: Any,
    *,
    tenant_id: str,
    principal: Any,
    decision_id: str,
    answer: str,
    expected_version: int,
    nonce: str,
    attach_as_source: bool = False,
) -> dict[str, Any]:
    store = orchestrator.store
    humans = PlanningHumanStore(store)
    plain = dict(decision_id=decision_id, tenant_id=tenant_id, principal=principal,
                 answer=answer, expected_version=expected_version, nonce=nonce)
    row = humans.get(decision_id)
    if not attach_as_source or row is None:
        return humans.answer(**plain)  # an unknown question is refused by the store itself
    if row["request"]["payload"]["options"]:
        raise ContractError("a choice answer cannot be attached as source material")
    mission_id = row["mission_id"]
    domain = orchestrator.commit.domain_for(mission_id)
    if not domain.source_roots:
        raise ContractError("this Mission does not accept source material")
    path = answer_source_path(domain.source_roots[0], decision_id)
    orchestrator.validate_source_storage(mission_id)  # reads only; refuses before anything is written
    with store.transaction():
        receipt = humans.answer(**plain, attached_source_path=path)
        source = orchestrator.commit.register_source(
            mission_id=mission_id, tenant_id=tenant_id, principal=principal, path=path,
            content=answer, kind=ANSWER_SOURCE_KIND,
            idempotency_key=f"planning-answer:{decision_id}",
        )
    return {**receipt, "source": {"path": source["path"], "version_hash": source["version_hash"]}}


__all__ = ("ANSWER_SOURCE_KIND", "answer_planning_question", "answer_source_path")
