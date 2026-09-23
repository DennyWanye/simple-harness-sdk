from __future__ import annotations

from agent_orchestrator.knowledge.predicates import PredicateRegistry
from agent_orchestrator.planning.htn.domain_package import DomainPackageInstaller
from agent_orchestrator.planning.htn.registry import SchemaCatalog, TaskTypeCatalog
from agent_orchestrator.planning.htn.seed_methods import load_domain
from agent_orchestrator.planning.htn.seed_methods.loader import install_domain
from agent_orchestrator.planning.htn.world import build_planning_world


def test_seed_domain_projects_to_h5_package_and_installs_with_catalogues() -> None:
    domain = load_domain("code")
    installer = DomainPackageInstaller()
    schemas = SchemaCatalog()
    predicates = PredicateRegistry()
    catalog = TaskTypeCatalog()
    install_domain(
        domain,
        schemas=schemas,
        predicates=predicates,
        catalog=catalog,
        package_installer=installer,
    )
    package = installer.get("code", 1)
    assert package is not None
    assert package.content_hash == domain.planning_package().content_hash
    assert len(package.methods) == len(domain.methods)


def test_production_planning_world_keeps_the_installed_domain_package() -> None:
    world = build_planning_world("h5-production", domains=("code",))
    assert world.package_installer is not None
    package = world.package_installer.get("code", 1)
    assert package is not None
    assert package.content_hash == world.domains[0].planning_package().content_hash
