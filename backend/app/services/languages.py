"""Static language detection from file names.

Only the file's exact name and its lower-cased extension are used: no file contents are
read and no filesystem access happens. Unknown types map to ``None``. Every language name
fits the ``files.language`` column (String(64)).
"""

# Exact file names, checked before the extension (e.g. CMakeLists.txt is CMake, not Text).
FILENAME_LANGUAGES: dict[str, str] = {
    "Dockerfile": "Dockerfile",
    "Containerfile": "Dockerfile",
    "Makefile": "Makefile",
    "makefile": "Makefile",
    "GNUmakefile": "Makefile",
    "CMakeLists.txt": "CMake",
    "Gemfile": "Ruby",
    "Rakefile": "Ruby",
    "Vagrantfile": "Ruby",
    "Jenkinsfile": "Groovy",
    "Procfile": "Procfile",
    "BUILD": "Starlark",
    "BUILD.bazel": "Starlark",
    "WORKSPACE": "Starlark",
    "go.mod": "Go Module",
    "requirements.txt": "Pip Requirements",
    ".gitignore": "Ignore List",
    ".dockerignore": "Ignore List",
    ".editorconfig": "EditorConfig",
}

# Lower-cased extensions without the leading dot.
EXTENSION_LANGUAGES: dict[str, str] = {
    # Programming languages
    "py": "Python",
    "pyi": "Python",
    "ipynb": "Jupyter Notebook",
    "js": "JavaScript",
    "mjs": "JavaScript",
    "cjs": "JavaScript",
    "jsx": "JavaScript",
    "ts": "TypeScript",
    "mts": "TypeScript",
    "cts": "TypeScript",
    "tsx": "TSX",
    "java": "Java",
    "kt": "Kotlin",
    "kts": "Kotlin",
    "scala": "Scala",
    "groovy": "Groovy",
    "gradle": "Gradle",
    "go": "Go",
    "rs": "Rust",
    "c": "C",
    "h": "C",
    "cpp": "C++",
    "cc": "C++",
    "cxx": "C++",
    "hpp": "C++",
    "hh": "C++",
    "hxx": "C++",
    "cs": "C#",
    "fs": "F#",
    "rb": "Ruby",
    "php": "PHP",
    "swift": "Swift",
    "m": "Objective-C",
    "mm": "Objective-C++",
    "dart": "Dart",
    "lua": "Lua",
    "r": "R",
    "pl": "Perl",
    "pm": "Perl",
    "ex": "Elixir",
    "exs": "Elixir",
    "erl": "Erlang",
    "hs": "Haskell",
    "clj": "Clojure",
    "jl": "Julia",
    "zig": "Zig",
    "sol": "Solidity",
    # Shell and scripting
    "sh": "Shell",
    "bash": "Shell",
    "zsh": "Shell",
    "ps1": "PowerShell",
    "psm1": "PowerShell",
    "bat": "Batchfile",
    "cmd": "Batchfile",
    # Data, query and markup
    "sql": "SQL",
    "html": "HTML",
    "htm": "HTML",
    "css": "CSS",
    "scss": "SCSS",
    "sass": "Sass",
    "less": "Less",
    "vue": "Vue",
    "svelte": "Svelte",
    "json": "JSON",
    "yaml": "YAML",
    "yml": "YAML",
    "toml": "TOML",
    "xml": "XML",
    "ini": "INI",
    "csv": "CSV",
    "svg": "SVG",
    "graphql": "GraphQL",
    "gql": "GraphQL",
    "proto": "Protocol Buffer",
    "tf": "HCL",
    "hcl": "HCL",
    "cmake": "CMake",
    "dockerfile": "Dockerfile",
    # Documentation
    "md": "Markdown",
    "markdown": "Markdown",
    "rst": "reStructuredText",
    "txt": "Text",
}


def detect_language(name: str, extension: str | None) -> str | None:
    """Language for a file named ``name`` with ``extension`` (without the dot), or None."""
    by_name = FILENAME_LANGUAGES.get(name)
    if by_name is not None:
        return by_name
    if not extension:
        return None
    return EXTENSION_LANGUAGES.get(extension.lower())
