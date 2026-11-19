"""The init container copies the mounted Secret into a memory volume, so the server container never mounts
the Secret itself (keys/stage.py)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from exchange_mcp.keys import stage


def _secret_volume(root):
    """The layout Kubernetes gives a Secret volume: a timestamped dir, a ..data link, one link per key."""
    data = root / "..2026_11_18_20_00_00.000000001"
    data.mkdir(parents=True)
    (data / "key").write_text("k1:" + "A" * 43 + "\n")
    (data / "adfs.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (root / "..data").symlink_to(data.name)
    for name in ("key", "adfs.pem"):
        (root / name).symlink_to(f"..data/{name}")
    return root


def test_each_key_is_copied_readable_by_its_owner_only(tmp_path):
    src = _secret_volume(tmp_path / "secret")
    src_mode = {n: (src / n).read_bytes() for n in ("key", "adfs.pem")}
    dst = tmp_path / "run"
    dst.mkdir()
    assert stage.copy(src, dst) == 2
    assert sorted(os.listdir(dst)) == ["adfs.pem", "key"]  # no ..data, no temporary files
    for name, body in src_mode.items():
        st = (dst / name).lstat()
        assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o400
        assert (dst / name).read_bytes() == body


def test_a_second_run_replaces_the_files(tmp_path):
    src = _secret_volume(tmp_path / "secret")
    dst = tmp_path / "run"
    dst.mkdir()
    stage.copy(src, dst)
    stage.copy(src, dst)  # a pod started again over a volume that survived
    assert sorted(os.listdir(dst)) == ["adfs.pem", "key"]


def test_an_empty_secret_stops_the_pod(tmp_path):
    src = tmp_path / "secret"
    src.mkdir()
    dst = tmp_path / "run"
    dst.mkdir()
    with pytest.raises(SystemExit) as e:
        stage.main([str(src), str(dst)])
    assert e.value.code == 2


def test_an_oversized_file_is_refused_and_nothing_is_left(tmp_path):
    src = tmp_path / "secret"
    src.mkdir()
    (src / "key").write_bytes(b"x" * (stage.MAX_SECRET_FILE + 1))
    dst = tmp_path / "run"
    dst.mkdir()
    with pytest.raises(SystemExit):
        stage.main([str(src), str(dst)])
    assert os.listdir(dst) == []


def test_it_prints_names_and_never_a_value(tmp_path, capsys):
    src = _secret_volume(tmp_path / "secret")
    dst = tmp_path / "run"
    dst.mkdir()
    stage.main([str(src), str(dst)])
    out = capsys.readouterr()
    assert "AAAA" not in out.out + out.err and "BEGIN" not in out.out + out.err
    lines = [json.loads(line) for line in out.err.splitlines()]
    assert out.out == "" and len(lines) == 1, "one JSON line on stderr, nothing else"
    assert lines[0]["event"] == "keys_staged" and lines[0]["count"] == 2
    assert lines[0]["files"] == ["adfs.pem", "key"]


def test_a_second_secret_is_staged_under_its_prefix(tmp_path):
    """The state store's passwords come from their own Secret and land beside the key, prefixed."""
    src = _secret_volume(tmp_path / "secret")
    state = tmp_path / "state"
    state.mkdir()
    (state / "password").write_text("store-password")
    dst = tmp_path / "run"
    dst.mkdir()
    stage.main([str(src), str(dst), f"{state}:state-"])
    assert sorted(os.listdir(dst)) == ["adfs.pem", "key", "state-password"]
    assert oct(os.stat(dst / "state-password").st_mode & 0o777) == "0o400"


def test_an_empty_second_secret_stops_the_pod(tmp_path):
    src = _secret_volume(tmp_path / "secret")
    state = tmp_path / "state"
    state.mkdir()
    dst = tmp_path / "run"
    dst.mkdir()
    with pytest.raises(SystemExit) as e:
        stage.main([str(src), str(dst), f"{state}:state-"])
    assert e.value.code == 2


def test_a_key_whose_name_starts_with_one_dot_is_staged(tmp_path):
    """Only `..` names are Kubernetes' own; `.key` is a valid Secret key."""
    src = tmp_path / "secret"
    data = src / "..2026_11_19_00_00_00.000000001"
    data.mkdir(parents=True)
    (data / ".key").write_text("k1:" + "B" * 43 + "\n")
    (src / "..data").symlink_to(data.name)
    (src / ".key").symlink_to("..data/.key")
    dst = tmp_path / "run"
    dst.mkdir()
    assert stage.copy(src, dst) == 1
    assert os.listdir(dst) == [".key"]


def test_a_short_write_is_completed_not_truncated(tmp_path, monkeypatch):
    """os.write may write fewer bytes than asked (a tmpfs near its limit); the file must still be whole."""
    src = _secret_volume(tmp_path / "secret")
    dst = tmp_path / "run"
    dst.mkdir()
    real = os.write
    monkeypatch.setattr(stage.os, "write", lambda fd, data: real(fd, bytes(data[:3])))
    stage.copy(src, dst)
    assert (dst / "key").read_bytes() == (src / "key").read_bytes()


def test_a_write_that_makes_no_progress_fails(tmp_path, monkeypatch):
    src = _secret_volume(tmp_path / "secret")
    dst = tmp_path / "run"
    dst.mkdir()
    monkeypatch.setattr(stage.os, "write", lambda fd, data: 0)
    with pytest.raises(SystemExit):
        stage.main([str(src), str(dst)])
    assert os.listdir(dst) == []


def test_a_temporary_file_left_by_a_killed_attempt_does_not_block_the_next(tmp_path):
    src = _secret_volume(tmp_path / "secret")
    dst = tmp_path / "run"
    dst.mkdir()
    left = dst / "..key.tmp"
    left.write_text("half")
    left.chmod(0o400)
    assert stage.copy(src, dst) == 2
    assert sorted(os.listdir(dst)) == ["adfs.pem", "key"]
