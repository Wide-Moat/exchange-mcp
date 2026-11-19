"""Run by the chart's init container: copy the mounted Secret into a memory volume.

The server container then mounts only that volume, never the Secret: what it holds is a file readable by
the server's user alone (0400), on a tmpfs that disappears with the pod. A container restart finds the
file still there, so sessions survive a crash; a new pod copies the Secret again.

    python -m exchange_mcp.keys.stage <secret dir>[:<prefix>] <memory dir> [<secret dir>:<prefix> ...]

Further Secrets are staged into the same memory directory under a prefix: the chart stages the shared
state store's passwords as `state-password` and `state-sentinelPassword`. It logs, as JSON lines, how
many files it copied and their names, never a value.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from exchange_mcp.keys.providers import MAX_SECRET_FILE

log = logging.getLogger("exchange_mcp.keys.stage")


def names_in(src: Path) -> list[str]:
    """The keys of a Secret volume. Names starting with `..` are Kubernetes' own."""
    return sorted(n for n in os.listdir(src) if not n.startswith("..") and (src / n).is_file())


def copy(src: Path, dst: Path, prefix: str = "") -> int:
    """Copy each key of a Secret volume to `dst`, mode 0400, replacing atomically. Returns how many.
    Names starting with `..` (`..data`, the timestamped dir) are Kubernetes' own; a key may start with
    one dot."""
    names = sorted(n for n in os.listdir(src) if not n.startswith("..") and (src / n).is_file())
    copied = 0
    for name in names:
        with (src / name).open("rb") as f:
            body = f.read(MAX_SECRET_FILE + 1)
        if len(body) > MAX_SECRET_FILE:
            raise ValueError(f"{name} is larger than {MAX_SECRET_FILE} bytes")
        target = prefix + name
        tmp = dst / f"..{target}.tmp"
        tmp.unlink(missing_ok=True)  # left by an attempt killed mid-copy; 0400, so it cannot be reopened
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        try:
            os.fchmod(fd, 0o400)  # the umask must not widen or narrow it
            view = memoryview(body)
            while view:
                n = os.write(fd, view)  # may write less, e.g. on a tmpfs near its size limit
                if n <= 0:
                    raise OSError("the key could not be written to the memory volume")
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, dst / target)
        copied += 1
    return copied


def main(argv: list[str] | None = None) -> None:
    from exchange_mcp.observability import configure

    configure()
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2 or any(":" not in a for a in args[2:]):
        log.error(
            "usage: python -m exchange_mcp.keys.stage <secret dir>[:<prefix>] <memory dir> [<secret dir>:<prefix> ...]",
            extra={"event": "stage_usage"},
        )
        raise SystemExit(2)
    dst = Path(args[1])
    sources = [(Path(a.split(":", 1)[0]), a.split(":", 1)[1] if ":" in a else "") for a in [args[0], *args[2:]]]
    # Two Secrets must not stage the same file name: the second would replace the first (a store password
    # written over the sealing key) and the server would fail later, far from the cause. A file left by an
    # earlier attempt of this same init container is replaced as before.
    try:
        planned = [prefix + n for src, prefix in sources for n in names_in(src)]
    except OSError as e:
        log.error("staging the keys failed: %s", e.__class__.__name__, extra={"event": "stage_failed"})
        raise SystemExit(1) from None
    twice = sorted({n for n in planned if planned.count(n) > 1})
    if twice:
        log.error("two Secrets stage the same file: %s", ", ".join(twice), extra={"event": "stage_collision"})
        raise SystemExit(2)
    for src, prefix in sources:
        try:
            n = copy(src, dst, prefix)
        except (OSError, ValueError) as e:
            for left in dst.glob("..*.tmp"):
                left.unlink(missing_ok=True)
            log.error("staging the keys failed: %s", e.__class__.__name__, extra={"event": "stage_failed"})
            raise SystemExit(1) from None
        if n == 0:
            # a Secret with no keys would start a server that cannot seal anything; stop the pod here instead
            log.error("no key files in %s: the Secret is empty or not mounted", src, extra={"event": "stage_empty"})
            raise SystemExit(2)
    names = sorted(p for p in os.listdir(dst) if not p.startswith(".."))
    log.info(
        "staged %d files into %s", len(names), dst, extra={"event": "keys_staged", "count": len(names), "files": names}
    )


if __name__ == "__main__":
    main()
