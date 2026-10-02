"""Dependency pin consistency tests (local only: no network, nothing installed).

constraints.txt records the exact versions of the verified backend environment; the
direct dependency declarations stay in requirements.txt and requirements-dev.txt. These
tests check the file's format, that it agrees with both requirements files, and that it
matches the environment the tests run in exactly (pip itself excluded). A mismatch means
the environment or the pins changed without the file being regenerated.
"""

import os
import re
import subprocess
import sys
from importlib.metadata import distributions
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONSTRAINTS = BACKEND_DIR / "constraints.txt"
PIN_LINE = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*)==([A-Za-z0-9.+!-]+)$")


def _pin_lines() -> list[str]:
    lines = CONSTRAINTS.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in _pin_lines():
        match = PIN_LINE.match(line)
        assert match, f"not a normalized name==version pin: {line!r}"
        pins[match.group(1)] = match.group(2)
    return pins


def _direct_requirements(file_name: str) -> list[Requirement]:
    requirements = []
    for raw in (BACKEND_DIR / file_name).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):  # "-r requirements.txt" is an include
            requirements.append(Requirement(line))
    return requirements


def _installed() -> dict[str, str]:
    installed: dict[str, str] = {}
    for dist in distributions():
        name = canonicalize_name(dist.metadata["Name"])
        if name != "pip":
            installed[name] = dist.version
    return installed


def test_every_line_is_a_normalized_exact_pin() -> None:
    for line in _pin_lines():
        match = PIN_LINE.match(line)
        assert match, f"not an exact pin: {line!r}"
        assert canonicalize_name(match.group(1)) == match.group(1), f"not normalized: {line!r}"


def test_pins_are_sorted_and_unique() -> None:
    names = [line.split("==", 1)[0] for line in _pin_lines()]
    assert names == sorted(names)
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    "forbidden",
    ["://", "/", "\\", "--hash", "-e ", "--index-url", "--extra-index-url", "-r ", "-c ", "@"],
)
def test_no_forbidden_entries(forbidden: str) -> None:
    for line in _pin_lines():
        assert forbidden not in line, f"forbidden {forbidden!r} in {line!r}"
        assert not line.startswith("-"), f"option line: {line!r}"


@pytest.mark.parametrize("file_name", ["requirements.txt", "requirements-dev.txt"])
def test_direct_requirements_are_pinned_at_the_same_version(file_name: str) -> None:
    pins = _pins()
    requirements = _direct_requirements(file_name)
    assert requirements, f"{file_name} declares no requirements"
    for requirement in requirements:
        name = canonicalize_name(requirement.name)  # e.g. uvicorn[standard] -> uvicorn
        specifiers = list(requirement.specifier)
        assert (
            len(specifiers) == 1 and specifiers[0].operator == "=="
        ), f"{file_name}: {requirement} is not an exact pin"
        assert (
            pins.get(name) == specifiers[0].version
        ), f"{file_name}: {requirement} does not match constraints ({pins.get(name)})"


def test_extras_are_compared_by_package_name() -> None:
    requirement = Requirement("uvicorn[standard]==0.29.0")
    assert canonicalize_name(requirement.name) == "uvicorn"
    assert requirement.extras == {"standard"}
    assert _pins()["uvicorn"] == "0.29.0"
    assert not any("[" in line for line in _pin_lines())  # pins never carry extras


def test_every_pin_is_installed_at_that_version() -> None:
    installed = _installed()
    mismatched = {
        name: (version, installed.get(name))
        for name, version in _pins().items()
        if installed.get(name) != version
    }
    assert mismatched == {}, f"pinned vs installed: {mismatched}"


def test_every_installed_distribution_is_pinned() -> None:
    pins = _pins()
    unpinned = {name: version for name, version in _installed().items() if name not in pins}
    assert unpinned == {}, f"installed but not pinned: {unpinned}"
    assert "pip" not in pins  # pip itself is deliberately excluded


def test_pip_check_reports_no_conflicts() -> None:
    """Environment integrity only (installed packages' declared requirements are met);
    this does not validate constraints.txt by itself. Runs offline."""
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INDEX": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
