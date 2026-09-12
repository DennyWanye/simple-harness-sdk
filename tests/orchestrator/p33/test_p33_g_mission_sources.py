# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""G exact Mission source tree oracle before implementation: CAS, multiversion,
immutable rebind/reconstruction, empty catalog and corrupted metadata rejection."""

import shutil
from copy import deepcopy

import pytest
from test_p33_source_commits import PATH, accept, change_source, produce, submit
from test_p33_source_commits import e_scenes as e_scenes

from agent_orchestrator.artifacts.workspace import WorkspaceManager
from agent_orchestrator.contracts import ContractError


def prepare(s):
    from agent_orchestrator.verification.mission_sources import prepare_mission_tree

    return prepare_mission_tree(
        s.store,
        s.mission,
        s.profile,
        s.cas,
        seed={"seed.txt": "original"},
        files={"REPORT.md": b"accepted"},
    )


def test_g_two_versions_same_path_survive_exact_catalog_and_rebuild(e_scenes):
    from agent_orchestrator.verification.mission_sources import ensure_mission_tree

    s = e_scenes(paths=(PATH, PATH))
    one = submit(s)
    produce(one)
    accept(one)
    old = s.store.list_knowledge(s.mission.id)[0].id
    change_source(s)
    two = submit(s, index=1, used=(old,))
    produce(two)
    accept(two)
    binding = prepare(s)
    entries = binding["mission_source_catalog"]["entries"]
    assert len(entries) == 2 and len({e["mounted_path"] for e in entries}) == 2
    assert {e["version"] for e in entries} == {
        one.envelope.claims[0].citations[0].version,
        two.envelope.claims[0].citations[0].version,
    }
    manager = WorkspaceManager(s.root / "views", artifact_store=s.cas)
    config = {**binding, "attempt_id": "original-view"}
    copy = ensure_mission_tree(s.store, s.mission, s.profile, manager, config)
    for entry in entries:
        assert copy.resolve(entry["mounted_path"]).read_bytes() == s.cas.read(entry["version"])
    original = {path: copy.resolve(path).read_bytes() for path in copy.list_files()}
    assert ensure_mission_tree(s.store, s.mission, s.profile, manager, config).root == copy.root
    shutil.rmtree(copy.root)
    reopened = WorkspaceManager(s.root / "views", artifact_store=s.cas)
    restored = ensure_mission_tree(s.store, s.mission, s.profile, reopened, config)
    assert {path: restored.resolve(path).read_bytes() for path in restored.list_files()} == original
    restored.resolve("seed.txt").write_bytes(b"tampered")
    with pytest.raises(ContractError):
        ensure_mission_tree(s.store, s.mission, s.profile, reopened, config)
    assert restored.resolve("seed.txt").read_bytes() == b"tampered"


@pytest.mark.parametrize(
    "damage", ["missing_catalog", "catalog_hash", "missing_trust", "tree_hash", "unsafe_view"]
)
def test_g_bad_frozen_metadata_refuses_without_creating_view(e_scenes, damage):
    from agent_orchestrator.verification.mission_sources import ensure_mission_tree

    s = e_scenes(paths=(PATH,))
    e = submit(s)
    produce(e)
    accept(e)
    config = {**deepcopy(prepare(s)), "attempt_id": "original-view"}
    if damage == "missing_catalog":
        config.pop("mission_source_catalog")
    elif damage == "catalog_hash":
        config["mission_source_catalog"]["hash"] = "0" * 64
    elif damage == "missing_trust":
        config["untrusted_sources"] = []
    elif damage == "unsafe_view":
        config["attempt_id"] = "../outside"
    else:
        config["mission_judge_tree"]["hash"] = "0" * 64
    manager = WorkspaceManager(s.root / "views", artifact_store=s.cas)
    with pytest.raises(ContractError):
        ensure_mission_tree(s.store, s.mission, s.profile, manager, config)
    assert not manager.root.exists() or list(manager.root.iterdir()) == []


def test_g_empty_catalog_is_explicit_and_reserved_mount_cannot_collide(e_scenes):
    from agent_orchestrator.verification.mission_sources import prepare_mission_tree

    s = e_scenes(paths=(PATH,))
    binding = prepare(s)
    assert binding["mission_source_catalog"]["entries"] == []
    assert len(binding["mission_source_catalog"]["hash"]) == 64
    with pytest.raises(ContractError, match="collision"):
        prepare_mission_tree(
            s.store, s.mission, s.profile, s.cas, seed={"MISSION-SOURCES/spoof": "fake"}, files={}
        )


@pytest.mark.parametrize("hidden", [".git", ".pytest_cache", "__pycache__"])
@pytest.mark.parametrize("symlink", [False, True])
def test_g_exact_tree_includes_normally_ignored_directories(e_scenes, hidden, symlink):
    from agent_orchestrator.verification.mission_sources import ensure_mission_tree

    s = e_scenes(paths=(PATH,))
    e = submit(s)
    produce(e)
    accept(e)
    manager = WorkspaceManager(s.root / "views", artifact_store=s.cas)
    config = {**prepare(s), "attempt_id": "original-view"}
    view = ensure_mission_tree(s.store, s.mission, s.profile, manager, config)
    directory = view.root / hidden
    directory.mkdir()
    intruder = directory / "extra.md"
    if symlink:
        intruder.symlink_to(s.root / "orchestrator.db")
    else:
        intruder.write_bytes(b"not in frozen tree")
    with pytest.raises(ContractError, match="tree"):
        ensure_mission_tree(s.store, s.mission, s.profile, manager, config)
    assert intruder.is_symlink() if symlink else intruder.read_bytes() == b"not in frozen tree"
