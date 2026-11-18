"""Fail when tracked files name a real organisation's data (manifesto: no installation in the product).

    python scripts/purity_check.py [root]

Looks at every file git tracks and reports, with file and line:

- an e-mail address outside the domains reserved for examples (example.com/.org/.net, corp.example,
  *.example, *.test, *.invalid, localhost);
- a public IPv4 address (private, loopback, link-local and the RFC 5737 documentation ranges pass);
- Cyrillic text outside the files listed in `.purity-translations` (the sign-in page's Russian
  locale): working text in this repository is English, and a Russian line elsewhere is usually a
  note about one installation.

Exit codes: 0 clean, 1 findings, 2 nothing was scanned (a check that read no file cannot pass).
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

EMAIL = re.compile(r"(?<![\w.%+-])[\w.%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
DOC_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
SELF = ("scripts/purity_check.py", "tests/unit/test_purity_check.py")


def _email_ok(domain: str) -> bool:
    d = domain.lower()
    if d in ("localhost", "example.com", "example.org", "example.net", "corp.example"):
        return True
    return d.endswith((".example", ".test", ".invalid", ".localhost", ".example.com", ".example.org", ".example.net"))


def _ip_ok(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True  # 999.1.1.1 is a version string, not an address
    # not is_global also covers ranges Python calls neither private nor global, such as 100.64.0.0/10
    return not ip.is_global or ip.is_multicast or any(ip in n for n in DOC_NETS)


def findings(line: str, translations_ok: bool = False) -> list[str]:
    out = [f"e-mail {m.group(0)}" for m in EMAIL.finditer(line) if not _email_ok(m.group(1))]
    out += [f"address {m.group(1)}" for m in IPV4.finditer(line) if not _ip_ok(m.group(1))]
    if not translations_ok and CYRILLIC.search(line):
        out.append("Cyrillic text outside .purity-translations")
    return out


def _translations(root: str) -> set[str]:
    path = Path(root, ".purity-translations")
    if not path.is_file():
        return set()
    lines = (ln.strip() for ln in path.read_text().splitlines())
    return {ln for ln in lines if ln and not ln.startswith("#")}


def main(root: str = ".") -> int:
    # A fixed argv, no shell; git comes from PATH as everywhere else in CI.
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=True).stdout  # noqa: S603, S607
    files = [f.decode() for f in out.split(b"\0") if f]
    translations = _translations(root)
    scanned, report = 0, []
    for name in files:
        path = Path(root, name)
        # This script and its test hold the patterns and the fixtures that must fail.
        if name in SELF or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        scanned += 1
        ok = name in translations
        for no, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            report += [f"{name}:{no}: {f}" for f in findings(line, ok)]
    print(f"purity: {scanned} of {len(files)} tracked files scanned")
    if scanned == 0:
        print("purity: nothing was scanned")
        return 2
    for r in report:
        print(r)
    print("purity: clean" if not report else f"purity: {len(report)} finding(s)")
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
