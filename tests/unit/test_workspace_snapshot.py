from app.infra.workspace_snapshot import diff_snapshots, take_snapshot


def test_detects_uncommitted_modification_new_untracked_and_removed_files(tmp_path):
    (tmp_path / "a.py").write_text("one")
    (tmp_path / "b.py").write_text("two")
    before = take_snapshot(tmp_path)

    (tmp_path / "a.py").write_text("changed")      # modified, never committed anywhere
    (tmp_path / "c.py").write_text("new")          # brand-new untracked file
    (tmp_path / "b.py").unlink()                   # removed
    after = take_snapshot(tmp_path)

    d = diff_snapshots(before, after)
    assert d.modified == {"a.py"} and d.added == {"c.py"} and d.removed == {"b.py"}
    assert before["code_fingerprint"] != after["code_fingerprint"]


def test_volatile_artifacts_do_not_change_the_code_fingerprint(tmp_path):
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "junit.xml").write_text("<a/>")
    s1 = take_snapshot(tmp_path)
    (tmp_path / "reports" / "junit.xml").write_text("<b timestamp='2'/>")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "x").write_text("1")
    s2 = take_snapshot(tmp_path)
    assert s1["code_fingerprint"] == s2["code_fingerprint"]
    assert not diff_snapshots(s1, s2).changed
    assert "reports/junit.xml" in s2["volatile"]


def test_git_directory_is_ignored(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    s = take_snapshot(tmp_path)
    assert list(s["files"]) == ["a.py"]


def test_manifest_has_hashes_only_so_it_cannot_restore_content(tmp_path):
    (tmp_path / "a.py").write_text("secret-body")
    s = take_snapshot(tmp_path)
    assert "secret-body" not in str(s)


# --- versioned code_fingerprint, fail-closed errors ---------------------------------------------------------------------------
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.infra import workspace_snapshot as ws
from app.infra.workspace_policy import WalkItem
from app.infra.workspace_snapshot import SnapshotError, is_current_snapshot_version
from app.validation.exceptions import FailClosed


def test_code_fingerprint_is_versioned_and_stable(tmp_path):
    (tmp_path / "a.py").write_text("x")
    first, second = take_snapshot(tmp_path), take_snapshot(tmp_path)
    assert first["code_fingerprint"] == second["code_fingerprint"]
    assert first["code_fingerprint"].startswith("STEP_SNAPSHOT_V1:")
    assert is_current_snapshot_version(first["code_fingerprint"])
    assert not is_current_snapshot_version("WORKSPACE_FINGERPRINT_V1:" + "0" * 64)      # another purpose's prefix
    assert not is_current_snapshot_version("a" * 64)                                       # the old unversioned value


def test_same_size_and_same_mtime_content_change_is_detected(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("aaaa")
    before = take_snapshot(tmp_path)
    stat = target.stat()
    target.write_text("bbbb")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert target.stat().st_size == stat.st_size and target.stat().st_mtime_ns == stat.st_mtime_ns
    assert take_snapshot(tmp_path)["code_fingerprint"] != before["code_fingerprint"]


def test_directory_links_are_not_descended_into_and_their_target_content_is_never_read(tmp_path):
    ws_dir, outside = tmp_path / "ws", tmp_path / "outside"
    ws_dir.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("one")
    (ws_dir / "a.txt").write_text("a")
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(ws_dir / "linkdir"), str(outside)], check=True,
                           capture_output=True)
        else:
            os.symlink(outside, ws_dir / "linkdir", target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("cannot create a directory link here")
    before = take_snapshot(ws_dir)
    (outside / "secret.txt").write_text("a much longer replacement")
    (outside / "new.txt").write_text("new")
    after = take_snapshot(ws_dir)
    assert after["code_fingerprint"] == before["code_fingerprint"] and "linkdir" in after["files"]
    assert not [k for k in after["files"] if k.startswith("linkdir/")]


def test_a_changed_volatile_result_does_not_invalidate_code_freshness(tmp_path):
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "junit.xml").write_text("<a/>")
    before = take_snapshot(tmp_path)
    (tmp_path / "reports" / "junit.xml").write_text("<b/>")
    assert take_snapshot(tmp_path)["code_fingerprint"] == before["code_fingerprint"]


# --- error boundary: original OSError in, SnapshotError from it out ------------------------------------------------------------------
def test_an_unreadable_file_is_a_snapshot_error_never_an_unreadable_placeholder(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("y")

    def denied(path):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(ws, "_hash_file", denied)
    with pytest.raises(SnapshotError) as err:
        take_snapshot(tmp_path)
    assert isinstance(err.value, FailClosed) and isinstance(err.value.__cause__, PermissionError)
    assert "read" in str(err.value) and str(tmp_path) in str(err.value)               # exc.filename is reported
    assert "UNREADABLE" not in str(err.value)


def test_the_same_unreadable_state_cannot_hide_a_content_change(tmp_path, monkeypatch):
    """The old code stored 'UNREADABLE' for any unreadable file, so a content change was invisible. Now neither
    snapshot completes: there is nothing equal to compare, hence nothing reusable."""
    target = tmp_path / "a.py"
    target.write_text("one")
    monkeypatch.setattr(ws, "_hash_file", lambda p: (_ for _ in ()).throw(PermissionError(13, "denied", str(p))))
    with pytest.raises(SnapshotError):
        take_snapshot(tmp_path)
    target.write_text("a completely different body")
    with pytest.raises(SnapshotError):
        take_snapshot(tmp_path)


def test_a_link_target_read_failure_is_a_snapshot_error(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")
    link = WalkItem("link", "l", tmp_path / "l")
    monkeypatch.setattr(ws, "walk_workspace", lambda root: iter([link]))
    monkeypatch.setattr(ws, "read_link_target", lambda p: (_ for _ in ()).throw(OSError("cannot readlink")))
    with pytest.raises(SnapshotError) as err:
        take_snapshot(tmp_path)
    assert isinstance(err.value.__cause__, OSError) and "readlink" in str(err.value)
    assert "unknown" in str(err.value) and str(tmp_path) in str(err.value)           # no filename: no guessed path


def test_missing_root_and_file_root_fail_before_anything_is_walked(tmp_path):
    with pytest.raises(SnapshotError) as missing:
        take_snapshot(tmp_path / "nope")
    assert isinstance(missing.value.__cause__, FileNotFoundError)
    a_file = tmp_path / "f.txt"
    a_file.write_text("x")
    with pytest.raises(SnapshotError) as not_dir:
        take_snapshot(a_file)
    assert isinstance(not_dir.value.__cause__, NotADirectoryError)


def test_an_enumeration_error_after_the_first_entry_returns_no_partial_manifest(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")

    def walker(root):
        yield WalkItem("file", "a.py", tmp_path / "a.py")                            # the first entry is fine ...
        raise PermissionError(13, "Access is denied", str(tmp_path / "locked"))     # ... the next directory is not

    monkeypatch.setattr(ws, "walk_workspace", walker)
    with pytest.raises(SnapshotError) as err:
        take_snapshot(tmp_path)
    assert isinstance(err.value.__cause__, PermissionError)
    assert str(tmp_path / "locked") in str(err.value) and "enumerate" in str(err.value)


def test_an_existing_snapshot_error_is_not_wrapped_again(tmp_path, monkeypatch):
    original = SnapshotError("already the domain error")

    def walker(root):
        raise original
        yield  # pragma: no cover

    monkeypatch.setattr(ws, "walk_workspace", walker)
    with pytest.raises(SnapshotError) as err:
        take_snapshot(tmp_path)
    assert err.value is original and err.value.__cause__ is None


def test_the_walk_helper_passes_the_original_oserror_through(tmp_path, monkeypatch):
    from app.infra import workspace_policy as policy
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x")
    real_walk = os.walk

    def failing_walk(root, followlinks=False, onerror=None):
        yield from [(str(root), ["sub"], ["a.txt"])]
        onerror(PermissionError(13, "Access is denied", str(Path(root) / "sub")))   # os.walk's own error callback

    (tmp_path / "a.txt").write_text("a")
    monkeypatch.setattr(policy.os, "walk", failing_walk)
    seen = []
    with pytest.raises(PermissionError) as err:                                    # not converted to any domain error
        for item in policy.walk_workspace(tmp_path):
            seen.append(item.rel)
    assert seen and err.value.filename == str(tmp_path / "sub")
    monkeypatch.setattr(policy.os, "walk", real_walk)
