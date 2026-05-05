"""BP.D.5 drift guards for Phase D auxiliary compliance modules."""

from __future__ import annotations

import inspect
from types import ModuleType

from backend.compliance_matrix import automotive, industrial, medical, military


COMPLIANCE_MODULES = (medical, automotive, industrial, military)


def test_compliance_matrix_modules_have_mandatory_header_disclaimer() -> None:
    for module in COMPLIANCE_MODULES:
        assert module.__doc__ is not None
        assert module.__doc__.startswith("BP.D.")
        assert "compliance matrix auxiliary check." in module.__doc__.splitlines()[0]
        assert (
            "This is an auxiliary check tool. AI-assisted output MUST be reviewed "
            "by\na human certified engineer."
        ) in module.__doc__
        assert "This module is advisory only." in module.__doc__
        assert "third-party" in module.__doc__
        assert "certified-engineer review" in module.__doc__
        assert module.MODULE_DISCLAIMER == (
            "This is an auxiliary check tool. AI-assisted output MUST be reviewed "
            "by a human certified engineer."
        )


def _module_functions(module: ModuleType) -> list[str]:
    return sorted(
        name for name, fn in inspect.getmembers(module, inspect.isfunction)
        if fn.__module__ == module.__name__
    )


def test_compliance_matrix_module_functions_use_auxiliary_prefix() -> None:
    for module in COMPLIANCE_MODULES:
        names = _module_functions(module)
        assert names
        assert all(name.startswith("_auxiliary_") for name in names), names
