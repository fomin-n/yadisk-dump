from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "yadisk_dump"


def _source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py"))


def test_source_contains_only_approved_static_urls() -> None:
    urls = set(re.findall(r"https?://[^\"'\s]+", _source_text()))
    assert urls == {
        "https://cloud-api.yandex.net",
        "https://yandex.ru/dev/disk/poligon/",
    }


def test_source_has_no_dynamic_execution_or_process_spawning() -> None:
    source = _source_text()
    for pattern in (r"\beval\(", r"\bexec\(", r"\bpickle\b", r"\bsubprocess\b", "shell=True"):
        assert re.search(pattern, source) is None


def test_api_wrapper_has_no_mutating_client_calls() -> None:
    source = (SOURCE / "api.py").read_text(encoding="utf-8")
    pattern = r"client\.(upload|remove|delete|mkdir|move|publish)\b"
    assert re.search(pattern, source) is None


def test_token_lines_do_not_print_or_log() -> None:
    for path in SOURCE.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "token" in line:
                assert not re.search(r"print|log", line, re.IGNORECASE)


def test_runtime_dependencies_are_exactly_the_two_approved_projects() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependency_block = re.search(
        r"^dependencies = \[\n(.*?)^\]\n",
        project,
        re.DOTALL | re.MULTILINE,
    )
    assert dependency_block is not None
    dependencies = re.findall(r'"([^"]+)"', dependency_block.group(1))
    assert dependencies == ["yadisk[sync-defaults]>=3.4,<4", "rich>=13,<15"]
