"""Break each safety rule on purpose and require the tests to notice.

A green suite proves nothing about a guard unless the suite goes red without it. Each mutation below
removes one guard; the named tests must then fail. A mutation whose text is no longer found is itself a
failure, so a refactor cannot silently retire a check. When two checks hold one rule, both are removed.

    python scripts/mutation_check.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "exchange_mcp"

# (what it protects, file, [(original, replacement), ...], test file that must fail)
MUTATIONS = [
    (
        "the guard asks for the person's word",
        "core/guard.py",
        [("if user_confirmed is not True:", "if False:")],
        "tests/unit/test_core.py",
    ),
    ("the bucket", "core/guard.py", [("if b.tokens < 1:", "if False:")], "tests/unit/test_core.py"),
    (
        "the recipient cap in the guard",
        "core/guard.py",
        [("if recipients > MAX_RECIPIENTS:", "if False:")],
        "tests/unit/test_core.py",
    ),
    (
        "only the boolean true confirms",
        "server.py",
        [("if p in args and not isinstance(args[p], bool):", "if False:")],
        "tests/unit/test_server.py",
    ),
    (
        "unknown arguments are refused",
        "server.py",
        [("        if unknown:\n", "        if False:\n")],
        "tests/unit/test_server.py",
    ),
    (
        "ids are strings",
        "server.py",
        [
            (
                "            if (\n                isinstance(v, (int, float))",
                "            if False and (\n                isinstance(v, (int, float))",
            )
        ],
        "tests/unit/test_server.py",
    ),
    (
        "send_draft needs the person's word",
        "tools/mail.py",
        [
            ('        c.rt.guard.confirmed(kind="mail", user_confirmed=user_confirmed)\n', ""),
            (
                'receipt = c.guard(kind="mail", user_confirmed=user_confirmed,',
                'receipt = c.guard(kind="mail", user_confirmed=True,',
            ),
        ],
        "tests/unit/test_mail.py",
    ),
    (
        "only drafts made here are sent",
        "tools/mail.py",
        [("        if not d.ours or not d.is_draft:\n", "        if False:\n")],
        "tests/unit/test_mail.py",
    ),
    (
        "only drafts made here are changed",
        "tools/mail.py",
        [("            if not current.ours or not current.is_draft:\n", "            if False:\n")],
        "tests/unit/test_mail.py",
    ),
    (
        "at most 20 recipients in a draft",
        "tools/mail.py",
        [("if len(to_l) + len(cc_l) + len(bcc_l) > MAX_RECIPIENTS:", "if False:")],
        "tests/unit/test_mail.py",
    ),
    (
        "an automatic reply needs the person's word",
        "tools/mail.py",
        [
            (
                'c.guard(kind="auto_reply", user_confirmed=user_confirmed)',
                'c.guard(kind="auto_reply", user_confirmed=True)',
            )
        ],
        "tests/unit/test_mail.py",
    ),
    (
        "deleting a folder needs the person's word",
        "tools/mail.py",
        [('        c.rt.guard.confirmed(kind="folder", user_confirmed=user_confirmed, target=folder)\n', "")],
        "tests/unit/test_mail.py",
    ),
    (
        "attachments are bare names",
        "tools/mail.py",
        [('if not _FILE_NAME.match(f) or f.startswith((".", "-", "~")) or f in (".", ".."):', "if False:")],
        "tests/unit/test_mail.py",
    ),
    (
        "distribution lists are refused",
        "core/resolve.py",
        [("if p.kind in GROUPS:", "if False:")],
        "tests/unit/test_mail.py",
    ),
    (
        "ambiguity lists candidates",
        "core/resolve.py",
        [("    if len(pool) > 1:\n", "    if False:\n")],
        "tests/unit/test_mail.py",
    ),
    ("the EWS allowlist", "ews/client.py", [("if operation not in ALLOWED:", "if False:")], "tests/unit/test_ews.py"),
    (
        "errors inside HTTP 200",
        "ews/client.py",
        [('if m.get("ResponseClass") == "Error":', "if False:")],
        "tests/unit/test_ews.py",
    ),
    (
        "no key material from the environment",
        "config.py",
        [("    if named:\n", "    if False:\n")],
        "tests/unit/test_keys.py",
    ),
    (
        "key files are closed",
        "keys/providers.py",
        [("    if mode & stat.S_IRWXO:\n        return True\n", "")],
        "tests/unit/test_keys.py",
    ),
    (
        "a blob opens only as its own kind",
        "keys/seal.py",
        [('if not isinstance(body, dict) or body.get("k") != kind:', "if not isinstance(body, dict):")],
        "tests/unit/test_keys.py",
    ),
    (
        "blobs expire",
        "keys/seal.py",
        [("if not isinstance(exp, int) or exp < now:", "if not isinstance(exp, int):")],
        "tests/unit/test_keys.py",
    ),
    (
        "a code is used once",
        "oauth/provider.py",
        [
            (
                'if p is None or p["client_id"] != client.client_id or authorization_code in self._used_codes:',
                'if p is None or p["client_id"] != client.client_id:',
            ),
            ("if p is None or authorization_code.code in self._used_codes:", "if p is None:"),
        ],
        "tests/unit/test_oauth.py",
    ),
    (
        "a code belongs to its client",
        "oauth/provider.py",
        [
            (
                'if p is None or p["client_id"] != client.client_id or authorization_code in self._used_codes:',
                "if p is None or authorization_code in self._used_codes:",
            )
        ],
        "tests/unit/test_oauth.py",
    ),
    (
        "redirect URIs are https or loopback",
        "oauth/provider.py",
        [("if not redirect_allowed(str(uri)):", "if False:")],
        "tests/unit/test_oauth.py",
    ),
    (
        "sign-in attempts are limited",
        "auth/limiter.py",
        [("if len(q) >= self._limits[kind]:", "if False:")],
        "tests/unit/test_signin.py",
    ),
    (
        "logins of another domain are refused",
        "auth/login.py",
        [("if given.lower() not in (domain_l, netbios):", "if False:")],
        "tests/unit/test_signin.py",
    ),
    (
        "password mode refresh is capped",
        "config.py",
        [('if mode == "password" and not 1 <= refresh_hours <= PASSWORD_REFRESH_MAX_HOURS:', "if False:")],
        "tests/unit/test_config.py",
    ),
    (
        "upstreams are https",
        "config.py",
        [('if u.scheme != "https" or not u.hostname or u.username or u.password:', "if False:")],
        "tests/unit/test_config.py",
    ),
    (
        "a link opens only for its person",
        "links/routes.py",
        [('if cred is None or person_key(cred.login) != link.get("p"):', "if cred is None:")],
        "tests/unit/test_links.py",
    ),
    (
        "logs drop content",
        "observability.py",
        [("if _SHAPES.search(message) or (record.exc_text and _SHAPES.search(record.exc_text)):", "if False:")],
        "tests/unit/test_observability.py",
    ),
    (
        "the AD FS key file is closed",
        "auth/adfs.py",
        [("        if exposed(path.stat()):\n", "        if False:\n")],
        "tests/unit/test_adfs.py",
    ),
    (
        "a refresh cannot switch people",
        "auth/adfs.py",
        [("if previous is not None and previous.login != login:", "if False:")],
        "tests/unit/test_adfs.py",
    ),
]


def run(selector: str) -> int:
    """pytest's exit code: 0 passed, 1 a test failed. Anything else (2 collection error, 4 usage, 5 no
    tests) is not evidence that a test caught the mutation, so it is reported as its own problem."""
    return subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", selector],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).returncode


def main() -> int:
    failures = []
    for what, rel, edits, selector in MUTATIONS:
        path = SRC / rel
        original = path.read_text()
        mutated = original
        for old, new in edits:
            if mutated.count(old) != 1:
                failures.append(f"{what}: the guarded code was not found exactly once in {rel}")
                break
            mutated = mutated.replace(old, new)
        else:
            try:
                path.write_text(mutated)
                code = run(selector)
            finally:
                path.write_text(original)
            status = {0: "NOT CAUGHT", 1: "caught"}.get(code, f"BROKEN({code})")
            print(f"{status:10} {what}")
            if code == 0:
                failures.append(f"{what}: tests still pass with the guard removed")
            elif code != 1:
                failures.append(f"{what}: pytest exited {code}, which is not a failing test")
    if run("tests/unit") != 0:
        failures.append("the suite does not pass after restoring the code")
    print(f"\n{len(MUTATIONS)} mutations, {len(failures)} problems")
    for f in failures:
        print(" -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
