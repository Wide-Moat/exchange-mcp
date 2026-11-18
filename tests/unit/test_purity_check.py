"""The purity gate rejects what it must and passes what it must; an empty repository is not clean."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("purity_check", ROOT / "scripts" / "purity_check.py")
purity = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purity)


@pytest.mark.parametrize(
    "line",
    [
        "write to someone@realcompany.com",
        "server at 8.8.8.8 answers",
        "allow 8.8.8.8/32 only",
        "see http://8.8.8.8/123",
        "Привет, это заметка",
    ],
)
def test_a_real_value_is_a_finding(line):
    assert purity.findings(line)


@pytest.mark.parametrize(
    "line",
    [
        "alex@corp.example and pat@example.com and x@mail.test",
        "10.0.0.5, 192.168.1.1, 127.0.0.1, 203.0.113.7, 0.0.0.0, 10.0.0.0/8, 0.0.0.0/0",
        "carrier NAT 100.64.0.1, broadcast 255.255.255.255",
        "version 1.2.3.4000",
        "plain English",
    ],
)
def test_an_example_value_passes(line):
    assert purity.findings(line) == []


def test_cyrillic_passes_only_in_a_listed_translation():
    assert purity.findings("Войти", translations_ok=True) == []
    assert purity.findings("Войти", translations_ok=False)


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        subprocess.run(["git", "-C", str(tmp_path), "add", name], check=True)
    return tmp_path


def test_the_gate_fails_a_repository_with_a_finding(tmp_path, capsys):
    root = _repo(tmp_path, {"a.md": "mail ops@realcompany.com\n", "b.md": "fine\n"})
    assert purity.main(str(root)) == 1
    assert "a.md:1: e-mail" in capsys.readouterr().out


def test_the_translation_list_is_read(tmp_path):
    root = _repo(tmp_path, {".purity-translations": "# ru\nloc/ru.json\n", "loc/ru.json": '{"a": "Войти"}\n'})
    assert purity.main(str(root)) == 0


def test_nothing_scanned_is_not_clean(tmp_path):
    root = _repo(tmp_path, {})
    assert purity.main(str(root)) == 2


def test_this_repository_is_clean():
    assert purity.main(str(ROOT)) == 0
