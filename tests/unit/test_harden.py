"""The server process is not dumpable: another process of its own user, such as a `kubectl exec` shell,
cannot read its memory, environment or open files through /proc, and no core dump is written."""

from __future__ import annotations

import subprocess
import sys
import textwrap

CHILD = textwrap.dedent(
    """
    import sys, time
    from exchange_mcp.keys.harden import non_dumpable
    print("set" if non_dumpable() else "unavailable", flush=True)
    time.sleep(30)
    """
)


def test_the_server_process_hides_its_memory_from_its_own_user():
    child = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.PIPE, text=True)
    try:
        state = child.stdout.readline().strip() if child.stdout else ""
        if sys.platform.startswith("linux"):
            assert state == "set"
            # a sibling process of the same uid, as a shell opened with kubectl exec would be
            peek = subprocess.run(
                [sys.executable, "-c", f"open('/proc/{child.pid}/environ','rb').read()"],
                capture_output=True,
                text=True,
            )
            assert peek.returncode != 0 and "PermissionError" in peek.stderr, peek.stderr
        else:
            assert state == "unavailable"  # macOS and others: a local run only, said so in the log
    finally:
        child.kill()
        child.wait()


def test_the_entry_point_hardens_before_it_reads_any_setting():
    import inspect

    from exchange_mcp import app

    src = inspect.getsource(app.main)
    assert "non_dumpable()" in src
    assert src.index("non_dumpable()") < src.index("Settings.from_env()")
