# SPDX-License-Identifier: Apache-2.0
"""Prepare TASK_CONTENT using the original leaf package and frozen Attempt inputs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..assurance.certificates import UseIdentity
from ..assurance.checks import CriterionPolicy
from ..assurance.codec import AssuranceError, decode, fingerprint
from ..assurance.evidence import build_catalogue
from ..assurance.refs import AssuranceRef, Pin
from ..assurance.review_input import REVIEW_INSTRUCTIONS, render_review_input
from ..assurance.reviews import REVIEW_CODEC_VERSION, AssuranceReviewBinding
from ..contracts.resolution import RequirementClass
from ..storage.assurance_blobs import read_pinned_blob
from ..storage.assurance_reads import (
    AssuranceReader,
    read_complete_evidence_snapshot,
    read_epochs_locked,
    require_epochs_locked,
)
from ..storage.assurance_store import AssuranceStore
from ..storage.htn_store import HtnStore
from .assurance_check_use import CurrentAuthority, _merge_reads, _permission, _require_same_permission
from .assurance_purpose_reviews import output_manifest_hash
from .assurance_review_pins import ensure_review_blob_pins
from .assurance_review_transport import assurance_formula, read_review_invocation_locked
from .completion_inputs import load_completion_result_inputs
from .leaf_acceptance import LeafAcceptanceAssembly
from .scoped_content_review import read_task_content_candidate


def ensure_task_content_review(
    commit: Any,
    *,
    tenant_id: str,
    result_id: str,
    principal_id: str,
    authority: CurrentAuthority,
    cas: Any,
    config: Mapping[str, Any],
    context_policy_ref: Pin,
    reservation: Any,
    maximum_blob_bytes: int = 32 * 1024 * 1024,
):
    """Internal original Critic builder. No policy approval or model judgement.

    Caller supplies an actual routed config and installed context identity. The
    original handoff validator remains required at dispatch; preparing readable
    material does not authorize any acceptance, execution or external action.
    """
    store = commit.store
    if store.connection.in_transaction:
        raise AssuranceError("REVIEW_PREPARATION_INSIDE_TRANSACTION")
    gate = commit._assurance_root_gate
    if gate is None:
        raise AssuranceError("ROOT_AUTHORITY_UNAVAILABLE")
    root = gate.require_execution()
    agent_config = config.get("agent_config", {})
    if agent_config.get("instructions") != REVIEW_INSTRUCTIONS:
        raise AssuranceError("REVIEW_TEMPLATE_UNREGISTERED")
    with store.read_view():
        result = store.get_result(result_id)
        if result is None:
            raise AssuranceError("SOURCE_UNAVAILABLE", result_id)
        mission_id, task_id = result.envelope.mission_id, result.envelope.task_id
        if AssuranceStore(store).lane(mission_id) != "ASSURANCE_1_1":
            raise AssuranceError("ASSURANCE_PROFILE_REQUIRED")
        reader = AssuranceReader(store, tenant_id=tenant_id, mission_id=mission_id)
        projection = read_task_content_candidate(store, mission_id, task_id, result_id)
        semantics = HtnStore(store)
        task = semantics.task_semantics_of(mission_id, task_id)
        frozen = load_completion_result_inputs(store, result)
        package = LeafAcceptanceAssembly(store, commit)._package(
            mission_id,
            task,
            projection.requirements,
            result_id,
            frozen.frozen.manifest_hash,
            (projection.producer_agent_id,),
            projection=projection,
            persist=False,
        )
        review_key = "assurance-content:" + fingerprint(
            {"mission_id": mission_id, "package": str(package.package_id)}
        )
        existing = store.connection.execute(
            "SELECT dispatch_intent_id FROM assurance_review_invocations WHERE mission_id=? AND review_key=? AND ordinal=1",
            (mission_id, review_key),
        ).fetchone()
        if existing is not None:
            invocation, prior_binding = read_review_invocation_locked(commit, reader, existing[0])
            if (
                prior_binding.to_json()["package_ref"]
                != Pin(str(package.package_id), 0, fingerprint(package.to_json())).to_json()
            ):
                raise AssuranceError("REVIEW_PACKAGE_BINDING_MISMATCH")
            prior_identity = UseIdentity(
                mission_id,
                "REVIEW",
                review_key,
                projection.scope.scope_id,
                principal_id,
                "DISCLOSE",
                root.root_incarnation_id,
            )
            _permission(
                authority,
                prior_identity,
                AssuranceRef.from_json(prior_binding.to_json()["subject"]["target"]),
                int(store.now * 1000),
            )
            return invocation
        scope_row = store.connection.execute(
            "SELECT scope_hash FROM operation_completion_scopes WHERE scope_id=? AND mission_id=?",
            (projection.scope.scope_id, mission_id),
        ).fetchone()
        scope_ref = AssuranceRef(
            "completion_scope", Pin(projection.scope.scope_id, 0, scope_row[0])
        )
        policy = store.connection.execute(
            "SELECT * FROM assurance_criterion_policies WHERE mission_id=? AND requirements_hash=? AND scope_hash=?",
            (mission_id, projection.requirements.content_hash(), scope_row[0]),
        ).fetchone()
        if policy is None:
            raise AssuranceError("CHECK_POLICY_UNRESOLVED")
        policy_ref = AssuranceRef(
            "check_policy", Pin(policy["policy_id"], 0, policy["policy_hash"])
        )
        policies = decode(reader.read_exact_metadata(policy_ref).body_json)["criteria"]
        if {p["criterion_id"] for p in policies} != set(package.criterion_catalogue()):
            raise AssuranceError("POLICY_CATALOGUE_MISMATCH")
        target = AssuranceRef("result", Pin(result_id, 0, fingerprint(result.envelope.to_json())))
        task_ref = AssuranceRef("task", Pin(task_id, task.contract_revision, task.content_hash()))
        requirements_ref = AssuranceRef(
            "requirements",
            Pin(
                str(projection.requirements.revision_id),
                projection.requirements.revision,
                projection.requirements.content_hash(),
            ),
        )
        refs = {
            target,
            task_ref,
            requirements_ref,
            scope_ref,
            policy_ref,
            AssuranceRef(
                "input_manifest", Pin(frozen.frozen.manifest_hash, 0, frozen.frozen.manifest_hash)
            ),
        }
        refs.update(
            AssuranceRef("artifact", Pin(item.id, item.revision, item.content_hash))
            for item in projection.artifacts
        )
        for document in policies:
            policy_contract = CriterionPolicy.from_json(document)
            refs.update(ref for group in policy_contract.any_check_sets for ref in group)
        epochs = read_epochs_locked(store.connection, mission_id)
        metadata = tuple(
            reader.read_exact_metadata(ref) for ref in sorted(refs, key=lambda ref: ref.key)
        )
        complete = read_complete_evidence_snapshot(reader, scope_id=projection.scope.scope_id)
        identity = UseIdentity(
            mission_id,
            "REVIEW",
            review_key,
            projection.scope.scope_id,
            principal_id,
            "DISCLOSE",
            root.root_incarnation_id,
        )
        now_ms = int(store.now * 1000)
        permissions = tuple(
            (ref, _permission(authority, identity, ref, now_ms))
            for ref in sorted(refs, key=lambda ref: ref.key)
        )
        reads = [item.read_item for item in metadata] + [item.read_item for item in complete]
        for _, permission in permissions:
            reads.extend((permission.access, permission.policy))
        catalogue = build_catalogue(review_key, refs)
        binding = AssuranceReviewBinding.from_json(
            {
                "schema_version": 2,
                "mission_id": mission_id,
                "review_key": review_key,
                "package_ref": Pin(
                    str(package.package_id), 0, fingerprint(package.to_json())
                ).to_json(),
                "round_no": 1,
                "subject": {
                    "purpose": "TASK_CONTENT",
                    "target": target.to_json(),
                    "owner_task_ref": task_ref.pin.to_json(),
                    "occurrence_id": projection.scope.occurrence_id,
                    "completion_scope_ref": scope_ref.pin.to_json(),
                    "method_instance_ref": None,
                    "input_manifest_hash": frozen.frozen.manifest_hash,
                    "output_manifest_hash": output_manifest_hash(
                        "TASK_CONTENT",
                        {
                            "result_id": result_id,
                            "port_claims": [
                                {"port_key": str(claim.port_key), "path": str(claim.path)}
                                for claim in frozen.port_claims
                            ],
                            "artifacts": [
                                {"id": item.id, "revision": item.revision,
                                 "content_hash": item.content_hash}
                                for item in projection.artifacts
                            ],
                        },
                    ),
                },
                "requirements_ref": requirements_ref.pin.to_json(),
                "criterion_ids": sorted(package.criterion_catalogue()),
                "mandatory_ids": sorted(
                    str(c.criterion_id)
                    for c in package.criteria
                    if c.requirement_class is RequirementClass.HARD_CONSTRAINT
                ),
                "formula": assurance_formula(package.success_expression),
                "check_requirements": policies,
                "evidence_catalogue": [item.to_json() for item in catalogue],
                "producer_agent_ids": list(package.producer_agent_ids),
                "reviewer_policy_ref": Pin(
                    REVIEW_CODEC_VERSION,
                    1,
                    fingerprint(
                        {"instructions": REVIEW_INSTRUCTIONS, "codec": REVIEW_CODEC_VERSION}
                    ),
                ).to_json(),
                "context_policy_ref": context_policy_ref.to_json(),
                "criterion_policy_ref": policy_ref.to_json(),
                "read_set": [item.to_json() for item in _merge_reads(reads)],
                "catalogue_hash": fingerprint([item.to_json() for item in catalogue]),
            }
        )
    blob_refs = {ref for ref in refs if ref.kind in {"source", "artifact"}}
    pins = ensure_review_blob_pins(commit, tenant_id=tenant_id, binding=binding, refs=blob_refs)
    try:
        materials = {
            item.ref: item.body_json.encode() for item in metadata if item.ref not in blob_refs
        }
        remaining, blobs = maximum_blob_bytes, []
        for ref in sorted(blob_refs, key=lambda ref: ref.key):
            blob = read_pinned_blob(
                reader,
                ref,
                pin_id=pins[ref],
                review_key=review_key,
                cas=cas,
                maximum_bytes=remaining,
                authorize=lambda r: (
                    _permission(authority, identity, r, int(store.now * 1000)).access
                ),
                now_ms=lambda: int(store.now * 1000),
            )
            remaining -= len(blob.data)
            materials[ref] = blob.data
            blobs.append(blob)
        frozen_config = dict(config)
        if (
            frozen_config.get("attempt_id", result.envelope.attempt_id)
            != result.envelope.attempt_id
        ):
            raise AssuranceError("REVIEW_RESULT_SOURCE_MISMATCH")
        frozen_config["attempt_id"] = result.envelope.attempt_id
        frozen_config["message"] = render_review_input(
            binding, package=package.to_json(), materials=materials
        )

        def require_current_locked() -> None:
            current_ms = int(store.now * 1000)
            if gate.require_execution() != root:
                raise AssuranceError("RECHECK_REQUIRED")
            require_epochs_locked(store.connection, mission_id, epochs, now_ms=current_ms)
            for item in metadata:
                if reader.read_exact_metadata(item.ref) != item:
                    raise AssuranceError("RECHECK_REQUIRED")
            for ref, previous in permissions:
                _require_same_permission(authority, identity, ref, previous, current_ms)
            for blob in blobs:
                blob.require_current_locked(
                    reader,
                    now_ms=current_ms,
                    authorize=lambda ref: _permission(authority, identity, ref, current_ms).access,
                )

        return commit.ensure_assurance_review_invocation(
            tenant_id=tenant_id,
            binding=binding,
            package=package,
            request_command_id="content-review:" + result_id,
            config=frozen_config,
            reservation=reservation,
            require_current_locked=require_current_locked,
        )
    except Exception:
        from .assurance_review_pins import release_failed_preparation

        release_failed_preparation(commit, binding)
        raise
