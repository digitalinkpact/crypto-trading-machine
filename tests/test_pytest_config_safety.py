"""CI guard: keep the warnings policy strict for this money-handling system.

A blanket ``ignore::DeprecationWarning`` (or ``ignore`` of any broad category)
would let deprecated pandas/numpy/pydantic APIs in our own code slip through CI
and break trade execution after a dependency bump. This test fails the build if
the strict policy in pytest.ini is ever weakened, so the protection can't be
silently reverted in a future commit.
"""
from __future__ import annotations

import configparser
from pathlib import Path

_PYTEST_INI = Path(__file__).resolve().parents[1] / "pytest.ini"

# Broad ``ignore`` filters that defeat the purpose of warnings-as-errors. A
# legitimate ignore MUST be scoped to a specific third-party module, e.g.
# ``ignore::DeprecationWarning:vectorbt(\.|$)`` — never a bare category.
_FORBIDDEN_BLANKET_IGNORES = {
    "ignore",
    "ignore::warning",
    "ignore::deprecationwarning",
    "ignore::pendingdeprecationwarning",
    "ignore::userwarning",
    "ignore::futurewarning",
}


def _filter_lines() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(_PYTEST_INI)
    raw = parser.get("pytest", "filterwarnings", fallback="")
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def test_pytest_ini_exists():
    assert _PYTEST_INI.is_file(), f"missing {_PYTEST_INI}"


def test_errors_on_warnings_by_default():
    # The first effective filter must promote all warnings to errors.
    assert "error" in _filter_lines(), (
        "pytest.ini must keep `error` in filterwarnings so warnings fail the "
        "build for this money-handling system."
    )


def test_no_blanket_ignore_reintroduced():
    offenders = [ln for ln in _filter_lines()
                 if ln.lower() in _FORBIDDEN_BLANKET_IGNORES]
    assert not offenders, (
        "Blanket warning ignore(s) found in pytest.ini: "
        f"{offenders}. Scope every ignore to a specific third-party module "
        "(e.g. `ignore::DeprecationWarning:vectorbt(\\.|$)`) instead."
    )


def test_ignores_are_module_scoped():
    # Every `ignore` filter must name a module (4th colon-separated field),
    # never just `ignore::SomeWarning`.
    for ln in _filter_lines():
        if not ln.lower().startswith("ignore"):
            continue
        parts = ln.split(":")
        module = parts[3].strip() if len(parts) >= 4 else ""
        assert module, (
            f"Unscoped ignore filter in pytest.ini: {ln!r}. Append a target "
            "module so it can't mask deprecations from our own code."
        )
