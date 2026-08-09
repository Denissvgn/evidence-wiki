"""Installable EvidenceWiki package."""

__version__ = "0.2.4"


def __getattr__(name: str) -> object:
    """Resolve the public API lazily so a bare ``import evidence_wiki`` stays cheap.

    Building the capability contract loads every packaged workspace script plus
    the orchestration module, so importing it eagerly here would make the cost of
    naming the package the cost of using all of it.
    """
    if name == "contract":
        # ``_contract``, not ``contract``: a submodule of that name would be bound
        # onto this package by the import system and would then shadow the
        # callable exported here. See the module docstring in ``_contract``.
        from ._contract import contract

        globals()["contract"] = contract  # later lookups skip this hook entirely
        return contract
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
