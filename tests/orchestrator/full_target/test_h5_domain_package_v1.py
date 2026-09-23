from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from agent_orchestrator.contracts.models import ContractError
from agent_orchestrator.planning.htn.domain_package import (
    DomainPackageInstaller,
    ObserverRegistrationV1,
    OperatorRegistrationV1,
    PlanningDomainPackageV1,
)

_BRANCH_CHECK = importlib.util.spec_from_file_location(
    "check_htn_core_domain_branches", Path("scripts/check_htn_core_domain_branches.py")
)
assert _BRANCH_CHECK is not None and _BRANCH_CHECK.loader is not None
_BRANCH_MODULE = importlib.util.module_from_spec(_BRANCH_CHECK)
_BRANCH_CHECK.loader.exec_module(_BRANCH_MODULE)
find_domain_branches = _BRANCH_MODULE.find_domain_branches


def package() -> PlanningDomainPackageV1:
    return PlanningDomainPackageV1(
        domain_id="drone-sim-v1",
        package_version=1,
        schemas=("drone.state@1",),
        predicates=("drone.ready@1",),
        task_types=("inspect@1", "move@1"),
        methods=("inspect-method@1",),
        observers=(ObserverRegistrationV1("drone-observer", ("drone.ready@1",), "drone.read"),),
        operators=(OperatorRegistrationV1("move-op", "move@1", ("drone.move",)),),
        capabilities=("drone.read", "drone.move"),
        definitions={
            "schemas:drone.state@1": {"type": "object", "required": ["position"]},
            "predicates:drone.ready@1": {"arguments": ["drone_id"]},
            "task_types:inspect@1": {"parameters": ["target"]},
            "task_types:move@1": {"parameters": ["destination"]},
            "methods:inspect-method@1": {
                "task_type": "inspect@1",
                "subtasks": ["move@1", "inspect@1"],
            },
        },
    )


def test_domain_package_round_trips_and_hashes() -> None:
    value = package()
    decoded = PlanningDomainPackageV1.from_json(value.to_json())
    assert decoded == value
    assert len(value.content_hash) == 64
    assert value.to_json()["content_hash"] == value.content_hash


def test_definition_content_changes_identity_and_stale_hash_is_refused() -> None:
    original = package()
    changed = replace(original, content_hash="", definitions={
        **original.definitions,
        "schemas:drone.state@1": {"type": "object", "required": ["position", "battery"]},
    })
    assert changed.content_hash != original.content_hash
    stale = changed.to_json()
    stale["content_hash"] = original.content_hash
    with pytest.raises(ContractError, match="content_hash"):
        PlanningDomainPackageV1.from_json(stale)


def test_installed_nested_definition_tampering_is_rejected_on_read() -> None:
    value = package()
    installer = DomainPackageInstaller()
    installer.install(value)
    value.definitions["schemas:drone.state@1"]["required"].append("unexpected")
    with pytest.raises(ContractError, match="changed after freezing"):
        installer.get(value.domain_id, value.package_version)


def test_install_is_idempotent_and_rejects_conflicting_duplicate() -> None:
    installer = DomainPackageInstaller()
    value = package()
    assert installer.install(value).status == "INSTALLED"
    assert installer.install(value).status == "ALREADY_INSTALLED"
    conflict = PlanningDomainPackageV1(
        domain_id=value.domain_id,
        package_version=value.package_version,
        schemas=("different@1",),
        predicates=value.predicates,
        task_types=value.task_types,
        methods=value.methods,
        observers=value.observers,
        operators=value.operators,
        capabilities=value.capabilities,
        definitions={
            **{
                key: definition
                for key, definition in value.definitions.items()
                if key != "schemas:drone.state@1"
            },
            "schemas:different@1": {"type": "object", "required": ["different"]},
        },
    )
    with pytest.raises(ContractError, match="conflicting"):
        installer.install(conflict)


def test_cross_references_and_capabilities_are_fail_closed() -> None:
    with pytest.raises(ContractError, match="unknown predicate"):
        PlanningDomainPackageV1(
            domain_id="code-v1",
            package_version=1,
            schemas=(),
            predicates=(),
            task_types=("task@1",),
            methods=(),
            observers=(ObserverRegistrationV1("obs", ("missing@1",), "read"),),
            operators=(),
            capabilities=("read",),
        )
    with pytest.raises(ContractError, match="capability"):
        PlanningDomainPackageV1(
            domain_id="code-v1",
            package_version=1,
            schemas=(),
            predicates=("p@1",),
            task_types=("task@1",),
            methods=(),
            observers=(ObserverRegistrationV1("obs", ("p@1",), "read"),),
            operators=(),
            capabilities=(),
        )


def test_installer_rejects_an_id_only_manifest() -> None:
    value = package()
    manifest = PlanningDomainPackageV1(
        domain_id=value.domain_id,
        package_version=value.package_version,
        schemas=value.schemas,
        predicates=value.predicates,
        task_types=value.task_types,
        methods=value.methods,
        observers=value.observers,
        operators=value.operators,
        capabilities=value.capabilities,
    )
    with pytest.raises(ContractError, match="actual domain definitions"):
        DomainPackageInstaller().install(manifest)


def test_three_domain_names_use_the_same_data_contract() -> None:
    for name in ("code-v1", "appworld-v1", "drone-sim-v1"):
        value = package()
        value = PlanningDomainPackageV1(
            domain_id=name,
            package_version=value.package_version,
            schemas=value.schemas,
            predicates=value.predicates,
            task_types=value.task_types,
            methods=value.methods,
            observers=value.observers,
            operators=value.operators,
            capabilities=value.capabilities,
            definitions=value.definitions,
        )
        assert DomainPackageInstaller().install(value).status == "INSTALLED"


def test_core_htn_has_no_domain_name_branch() -> None:
    assert find_domain_branches(Path("src/agent_orchestrator/planning/htn")) == []


def test_domain_branch_scan_checks_attributes_reversed_membership_and_expressions(tmp_path) -> None:
    source = tmp_path / "core.py"
    source.write_text('''
def choose(package, domain_id, domain, values):
    if package.domain_id == "code-v1":
        return 1
    if "appworld-v1" == domain_id:
        return 2
    if domain in ("drone-sim-v1", "code-v1"):
        return 3
    x = 1 if package.domain == "code" else 0
    return [x for x in values if x.domain_id == "appworld"]
''')
    assert len(find_domain_branches(tmp_path)) == 5
    source.write_text('''
def generic(package, requested):
    if package.domain_id == requested:
        return package
    if package.domain is None:
        return None
''')
    assert find_domain_branches(tmp_path) == []
