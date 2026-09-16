# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Reading a seed domain from JSON and putting it through the admission protocol.

A domain is a directory of four files — ``schemas.json``, ``predicates.json``,
``task_types.json``, ``methods.json`` — and nothing else.  :func:`available_domains`
*scans* for them rather than listing known names, so the two pilot domains and a
third one a test invents take exactly the same path; there is no branch anywhere
in this package on which domain is being loaded.

One convention is local to the seed pack and stated rather than assumed: a
``VersionedRef`` written in these files may omit its ``content_hash``, and
:func:`seed_content_hash` fills it in from ``(id, version)``.  A method's own
``method_ref`` is still a real hash of its own bytes — that one is computed by
:meth:`~agent_orchestrator.contracts.htn.MethodContract.method_ref` — but a
*forward* reference from one file to a declaration in another cannot be, and
copying sixty-four hex digits between four files by hand is a way to ship a
fixture that silently fails to resolve.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ....contracts.htn import MethodContract, RegistryAuthor
from ....contracts.models import ContractError
from ....contracts.semantic_base import VersionedRef, sequence_of
from ....knowledge.predicates import PredicateRegistry, PredicateSignature
from ..registry import (
    AdmissionPolicy,
    AdmissionReceipt,
    MethodProposal,
    MethodRegistry,
    ObjectSchema,
    SchemaCatalog,
    TaskTypeCatalog,
    TaskTypeSpec,
)

#: Where the shipped domains live.  Every public function takes a ``root`` so a
#: caller can load a domain from anywhere — which is how a test registers a third,
#: entirely invented domain without touching this package.
SEED_ROOT = Path(__file__).resolve().parent

#: The four files a domain directory must contain.
DOMAIN_FILES = ("schemas.json", "predicates.json", "task_types.json", "methods.json")


def seed_content_hash(identifier: str, version: int) -> str:
    """The derived content hash of a seed declaration.

    Derived from ``(id, version)`` so the four files of a domain agree without
    anyone transcribing a digest.  This is a *fixture* convention: a production
    registry hashes the declaration's own bytes, and nothing in this function
    should be read as a claim that these ids are content-addressed.
    """

    return hashlib.sha256(f"{identifier}@{int(version)}".encode()).hexdigest()


def fill_content_hashes(node: Any) -> Any:
    """Return ``node`` with every ``{id, version}`` reference given a content hash."""

    if isinstance(node, list):
        return [fill_content_hashes(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {key: fill_content_hashes(value) for key, value in node.items()}
    if (
        "id" in out
        and "version" in out
        and "content_hash" not in out
        and isinstance(out["id"], str)
        and isinstance(out["version"], int)
    ):
        out["content_hash"] = seed_content_hash(out["id"], out["version"])
    return out


@dataclass(frozen=True, slots=True)
class SeedDomain:
    """One domain's declarations, decoded but not yet registered."""

    name: str
    schemas: tuple[ObjectSchema, ...]
    predicates: tuple[PredicateSignature, ...]
    task_types: tuple[TaskTypeSpec, ...]
    methods: tuple[MethodContract, ...]
    source: Path | None = None

    @property
    def capability_ids(self) -> tuple[str, ...]:
        """Every capability this domain's data mentions, in a stable order.

        A caller builds its :class:`~..applicability.CapabilitySnapshot` from this
        rather than from a hard-coded list, so a domain that needs a new tool says
        so in its own data.
        """

        out: list[str] = []
        for spec in self.task_types:
            out.extend(spec.required_capabilities)
        for method in self.methods:
            out.extend(method.required_capabilities)
            for step in method.steps:
                out.extend(step.required_capabilities)
        return tuple(dict.fromkeys(out))

    @property
    def observer_types(self) -> tuple[TaskTypeSpec, ...]:
        """The read-only types that can produce evidence for a predicate (§7.3)."""

        return tuple(spec for spec in self.task_types if spec.observes)

    @property
    def observed_predicates(self) -> tuple[VersionedRef, ...]:
        out: list[VersionedRef] = []
        for spec in self.observer_types:
            for ref in spec.observes:
                if ref not in out:
                    out.append(ref)
        return tuple(out)


def _read(path: Path) -> Any:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"seed file {path} is unreadable: {error}") from error
    return fill_content_hashes(raw)


def load_domain_path(path: Path) -> SeedDomain:
    """Decode the domain in ``path``.  Refuses rather than half-loading."""

    directory = Path(path)
    missing = [item for item in DOMAIN_FILES if not (directory / item).is_file()]
    if missing:
        raise ContractError(
            f"{directory} is not a seed domain: missing {', '.join(sorted(missing))}"
        )
    schemas = sequence_of(
        _read(directory / "schemas.json"),
        "schemas",
        lambda item, where: ObjectSchema.from_json(item, where),
    )
    predicates = sequence_of(
        _read(directory / "predicates.json"),
        "predicates",
        lambda item, where: PredicateSignature.from_json(item, where),
    )
    task_types = sequence_of(
        _read(directory / "task_types.json"),
        "task_types",
        lambda item, where: TaskTypeSpec.from_json(item, where),
    )
    methods = sequence_of(
        _read(directory / "methods.json"),
        "methods",
        lambda item, where: MethodContract.from_json(item, where),
    )
    return SeedDomain(
        name=directory.name,
        schemas=schemas,
        predicates=predicates,
        task_types=task_types,
        methods=methods,
        source=directory,
    )


def available_domains(root: Path = SEED_ROOT) -> tuple[str, ...]:
    """Every domain directory under ``root``, sorted.

    Found by looking, not by a list in this file: that is what makes "a third
    domain needs data only" a checkable statement rather than a claim.
    """

    directory = Path(root)
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            item.name
            for item in directory.iterdir()
            if item.is_dir() and all((item / file).is_file() for file in DOMAIN_FILES)
        )
    )


def load_domain(name: str, *, root: Path = SEED_ROOT) -> SeedDomain:
    return load_domain_path(Path(root) / name)


def install_domain(
    domain: SeedDomain,
    *,
    schemas: SchemaCatalog,
    predicates: PredicateRegistry,
    catalog: TaskTypeCatalog,
) -> None:
    """Register the domain's schemas, predicates and task types.

    Methods are deliberately *not* registered here: they go through
    :func:`admit_domain`, because a method that skipped the admission protocol
    would be exactly the bypass §7.3 exists to close — even a human-authored one.
    """

    for schema in domain.schemas:
        schemas.register(schema)
    for signature in domain.predicates:
        predicates.register(signature)
    for spec in domain.task_types:
        catalog.register(spec)


def admit_domain(
    domain: SeedDomain,
    *,
    registry: MethodRegistry,
    policy: AdmissionPolicy,
    author: RegistryAuthor = RegistryAuthor.HUMAN,
) -> tuple[AdmissionReceipt, ...]:
    """Submit every method of the domain through §7.3, in a stable order.

    The seed methods are hand-written, so they are submitted as ``HUMAN`` — the
    admission protocol treats that exactly like ``SYSTEM``, because the rule §7.3
    states is about a *model* promoting itself, but the receipt then records who
    actually wrote the method rather than attributing it to the system.
    """

    return tuple(
        registry.admit(
            MethodProposal(method=method, author=author),
            author=author,
            policy=policy,
        )
        for method in sorted(domain.methods, key=lambda item: (item.method_id, item.method_version))
    )


def install_library(
    names: Sequence[str] | None = None,
    *,
    root: Path = SEED_ROOT,
    schemas: SchemaCatalog,
    predicates: PredicateRegistry,
    catalog: TaskTypeCatalog,
) -> tuple[SeedDomain, ...]:
    """Load and install several domains at once, returning what was installed."""

    chosen = tuple(names) if names is not None else available_domains(root)
    domains = tuple(load_domain(name, root=root) for name in chosen)
    for domain in domains:
        install_domain(domain, schemas=schemas, predicates=predicates, catalog=catalog)
    return domains


def domain_capability_ids(domains: Sequence[SeedDomain]) -> tuple[str, ...]:
    out: list[str] = []
    for domain in domains:
        out.extend(domain.capability_ids)
    return tuple(dict.fromkeys(out))


def seed_ref(identifier: str, version: int = 1) -> VersionedRef:
    """A reference into the seed pack, hashed the way the loader hashes one."""

    return VersionedRef(
        id=identifier, version=version, content_hash=seed_content_hash(identifier, version)
    )


def describe(domain: SeedDomain) -> Mapping[str, Any]:
    """A small, deterministic summary for journals and diagnostics."""

    return {
        "domain": domain.name,
        "schemas": len(domain.schemas),
        "predicates": len(domain.predicates),
        "task_types": len(domain.task_types),
        "methods": len(domain.methods),
        "observer_types": len(domain.observer_types),
        "capabilities": list(domain.capability_ids),
    }


__all__ = (
    "DOMAIN_FILES",
    "SEED_ROOT",
    "SeedDomain",
    "admit_domain",
    "available_domains",
    "describe",
    "domain_capability_ids",
    "fill_content_hashes",
    "install_domain",
    "install_library",
    "load_domain",
    "load_domain_path",
    "seed_content_hash",
    "seed_ref",
)
