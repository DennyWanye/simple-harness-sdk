# SPDX-License-Identifier: Apache-2.0
"""2026-09-25 UI 全量点击：最终审查碰上一次模型服务端错误（provider_server_error），
这一轮没提交（TURN_FAILED），以前没有任何后续工作，任务约 5 秒后被判停滞失败。
现在它和"格式不对"一样，在同一份冻结材料上获得唯一一次第二次调用（TURN_RETRY）。"""

from types import SimpleNamespace

from agent_orchestrator.assurance.refs import AssuranceRef, Pin
from agent_orchestrator.assurance.reviews import SECOND_INVOCATION_REASONS
from agent_orchestrator.orchestrator.assurance_review_consumer import AssuranceReviewConsumer


def _event(kind: str, classification: str, ordinal: int = 1):
    ref = AssuranceRef("commit_receipt", Pin("receipt-1", 0, "a" * 64)).to_json()
    return SimpleNamespace(type=kind, payload={
        "classification": classification, "classification_receipt_ref": ref,
        "review_key": "rk", "invocation_ordinal": ordinal,
    })


def test_a_failed_review_turn_schedules_the_one_second_invocation():
    consumer = SimpleNamespace()
    targets = AssuranceReviewConsumer.classify(consumer, _event("AssuranceReviewClassified", "TURN_FAILED"))
    assert [t.work_key for t in targets] == ["review-import:rk:1"]
    # formats and ready reviews still route as before; other classifications do not
    assert AssuranceReviewConsumer.classify(consumer, _event("AssuranceReviewFormatRejected", "FORMAT_INVALID"))
    assert AssuranceReviewConsumer.classify(consumer, _event("AssuranceReviewClassified", "READY_FOR_CURRENT_REVIEW"))
    assert AssuranceReviewConsumer.classify(consumer, _event("AssuranceReviewClassified", "MODEL_IDENTITY_MISMATCH")) == ()


def test_second_invocation_reasons():
    assert SECOND_INVOCATION_REASONS == {"FORMAT_REPAIR", "TURN_RETRY"}
