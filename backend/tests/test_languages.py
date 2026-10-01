"""Language detection tests (no database, no filesystem)."""

import builtins
import os
from typing import Any

import pytest

from app.services.languages import EXTENSION_LANGUAGES, FILENAME_LANGUAGES, detect_language


@pytest.mark.parametrize(
    ("name", "extension", "expected"),
    [
        ("app.py", "py", "Python"),
        ("types.pyi", "pyi", "Python"),
        ("index.js", "js", "JavaScript"),
        ("main.ts", "ts", "TypeScript"),
        ("App.tsx", "tsx", "TSX"),
        ("Main.java", "java", "Java"),
        ("main.go", "go", "Go"),
        ("lib.rs", "rs", "Rust"),
        ("util.c", "c", "C"),
        ("util.hpp", "hpp", "C++"),
        ("Program.cs", "cs", "C#"),
        ("deploy.sh", "sh", "Shell"),
        ("query.sql", "sql", "SQL"),
        ("index.html", "html", "HTML"),
        ("config.yml", "yml", "YAML"),
        ("config.yaml", "yaml", "YAML"),
        ("package.json", "json", "JSON"),
        ("README.md", "md", "Markdown"),
        ("notes.txt", "txt", "Text"),
    ],
)
def test_known_extensions(name: str, extension: str, expected: str) -> None:
    assert detect_language(name, extension) == expected


@pytest.mark.parametrize("extension", ["PY", "Py", "pY"])
def test_extension_matching_is_case_insensitive(extension: str) -> None:
    assert detect_language(f"module.{extension}", extension) == "Python"


@pytest.mark.parametrize(
    ("name", "extension", "expected"),
    [
        ("Dockerfile", None, "Dockerfile"),
        ("Makefile", None, "Makefile"),
        ("GNUmakefile", None, "Makefile"),
        ("Jenkinsfile", None, "Groovy"),
        ("Gemfile", None, "Ruby"),
        ("CMakeLists.txt", "txt", "CMake"),  # exact name wins over the .txt extension
        ("requirements.txt", "txt", "Pip Requirements"),
        ("go.mod", "mod", "Go Module"),
        (".gitignore", None, "Ignore List"),
        (".editorconfig", None, "EditorConfig"),
    ],
)
def test_exact_filenames(name: str, extension: str | None, expected: str) -> None:
    assert detect_language(name, extension) == expected


@pytest.mark.parametrize(
    ("name", "extension"),
    [
        ("LICENSE", None),  # no extension, not a known file name
        (".env", None),  # unknown dotfile
        ("archive.unknownext", "unknownext"),
        ("data.bin", "bin"),
        ("image.png", "png"),
        ("dockerfile", None),  # file names match exactly (case-sensitive)
        ("noext", ""),
    ],
)
def test_unknown_types_return_none(name: str, extension: str | None) -> None:
    assert detect_language(name, extension) is None


def test_mapping_tables_are_well_formed() -> None:
    for extension in EXTENSION_LANGUAGES:
        assert extension == extension.lower() and not extension.startswith(".")
    for language in [*EXTENSION_LANGUAGES.values(), *FILENAME_LANGUAGES.values()]:
        assert 0 < len(language) <= 64  # fits files.language (String(64))


def test_detection_never_touches_the_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("language detection must not access the filesystem")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "stat", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    assert detect_language("app.py", "py") == "Python"
    assert detect_language("Dockerfile", None) == "Dockerfile"
    assert detect_language("unknown.zzz", "zzz") is None
