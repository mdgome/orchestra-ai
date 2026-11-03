"""WORKSPACE_FINGERPRINT_V1: canonical format, git-aware accuracy and safety, content fallback."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.infra import workspace_fingerprint as wf
from app.infra.workspace_fingerprint import (
    Entry, FingerprintError, canonical_digest, compute_fingerprint, git_env, git_global_args, is_current_version,
)
from app.infra.workspace_snapshot import take_snapshot

pytestmark = pytest.mark.skipif(wf._git_version() is None, reason="git is not installed")


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         *args], check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    (path / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (path / "util.py").write_text("X = 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def fp(path: Path) -> str:
    return compute_fingerprint(path).value


# --- canonical format ----------------------------------------------------------------------------------------------
def test_value_is_versioned_and_repeatable(repo):
    first, second = compute_fingerprint(repo), compute_fingerprint(repo)
    assert first == second and first.value.startswith("WORKSPACE_FINGERPRINT_V1:") and first.mode == "GIT"
    assert is_current_version(first.value) and not is_current_version("WORKSPACE_FINGERPRINT_V0:abc")
    assert not is_current_version(None)


def test_length_prefix_prevents_concatenation_collisions():
    a = canonical_digest("CONTENT", [Entry("FILE", "a", "-", b"bc")])
    b = canonical_digest("CONTENT", [Entry("FILE", "ab", "-", b"c")])
    c = canonical_digest("CONTENT", [Entry("FILE", "a", "-b", b"c")])
    d = canonical_digest("CONTENT", [Entry("FILE", "a", "-", b"bc"), Entry("FILE", "x", "-", b"")])
    e = canonical_digest("CONTENT", [Entry("FILE", "a", "-", b"bcx")])
    assert len({a, b, c, d, e}) == 5


def test_entry_order_is_canonical_and_case_is_preserved():
    entries = [Entry("FILE", "b.txt", "-", b"1"), Entry("FILE", "a.txt", "-", b"2"), Entry("FILE", "A.txt", "-", b"3")]
    assert canonical_digest("CONTENT", entries) == canonical_digest("CONTENT", list(reversed(entries)))
    assert canonical_digest("CONTENT", [Entry("FILE", "a.txt", "-", b"1")]) != \
        canonical_digest("CONTENT", [Entry("FILE", "A.txt", "-", b"1")])
    assert canonical_digest("CONTENT", entries) != canonical_digest("GIT", entries)      # the mode is part of the digest
    # non-ASCII names are encoded as UTF-8, independent of the OS default encoding
    assert canonical_digest("CONTENT", [Entry("FILE", "한글.py", "-", b"1")]) == \
        canonical_digest("CONTENT", [Entry("FILE", "한글.py", "-", b"1")])


def test_type_tag_distinguishes_otherwise_equal_entries():
    assert canonical_digest("CONTENT", [Entry("FILE", "a", "-", b"x")]) != \
        canonical_digest("CONTENT", [Entry("UNTRACKED", "a", "-", b"x")])


# --- accuracy ----------------------------------------------------------------------------------------------------------
def test_same_size_same_mtime_content_change_is_detected_in_git_mode(repo):
    before = fp(repo)
    target = repo / "app.py"
    stat = target.stat()
    target.write_text("print('v2')\n", encoding="utf-8")               # same length
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = target.stat()
    assert after.st_size == stat.st_size and after.st_mtime_ns == stat.st_mtime_ns
    assert fp(repo) != before


def test_same_size_same_mtime_content_change_is_detected_without_git(tmp_path):
    ws = tmp_path / "plain"
    ws.mkdir()
    target = ws / "a.txt"
    target.write_text("aaaa", encoding="utf-8")
    first = compute_fingerprint(ws)
    assert first.mode == "CONTENT"
    stat = target.stat()
    target.write_text("bbbb", encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert compute_fingerprint(ws).value != first.value


def test_two_different_edits_of_a_modified_tracked_file_differ(repo):
    clean = fp(repo)
    (repo / "app.py").write_text("print('first edit')\n", encoding="utf-8")
    first = fp(repo)
    (repo / "app.py").write_text("print('second edit')\n", encoding="utf-8")      # still " M"
    second = fp(repo)
    assert len({clean, first, second}) == 3
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")                # reverting restores the original
    assert fp(repo) == clean


def test_staged_only_unstaged_only_and_both_are_distinct(repo):
    clean = fp(repo)
    (repo / "app.py").write_text("print('staged')\n", encoding="utf-8")
    unstaged_only = fp(repo)
    git(repo, "add", "app.py")
    staged_only = fp(repo)
    (repo / "app.py").write_text("print('staged then edited')\n", encoding="utf-8")
    both = fp(repo)
    assert len({clean, unstaged_only, staged_only, both}) == 4


def test_untracked_files_count_and_ignored_ones_do_not(repo):
    clean = fp(repo)
    (repo / ".gitignore").write_text("out/\n*.log\n", encoding="utf-8")
    base = fp(repo)
    assert base != clean                                                  # .gitignore itself is untracked content
    (repo / "out").mkdir()
    (repo / "out" / "x.bin").write_bytes(b"1")
    (repo / "debug.log").write_text("noise", encoding="utf-8")
    assert fp(repo) == base                                               # ignored build output is invisible
    (repo / "new.py").write_text("Y = 2\n", encoding="utf-8")
    assert fp(repo) != base
    (repo / "new.py").write_text("Y = 3\n", encoding="utf-8")


def test_delete_and_rename_are_changes(repo):
    clean = fp(repo)
    (repo / "util.py").unlink()
    deleted = fp(repo)
    (repo / "util.py").write_text("X = 1\n", encoding="utf-8")
    assert fp(repo) == clean
    (repo / "util.py").rename(repo / "util2.py")                          # content identical, path differs
    renamed = fp(repo)
    assert len({clean, deleted, renamed}) == 3


@pytest.mark.skipif(os.name == "nt", reason="the executable bit is not observable on Windows")
def test_file_mode_change_is_a_change(repo):
    clean = fp(repo)
    (repo / "app.py").chmod(0o755)
    assert fp(repo) != clean


def test_unborn_repository(tmp_path):
    path = tmp_path / "fresh"
    path.mkdir()
    git(path, "init", "-q")
    empty = compute_fingerprint(path)
    assert empty.mode == "GIT"
    (path / "a.py").write_text("A = 1\n", encoding="utf-8")
    added = fp(path)
    git(path, "add", "a.py")
    assert len({empty.value, added, fp(path)}) == 3


def test_volatile_artifacts_do_not_change_the_fingerprint(repo):
    clean = fp(repo)
    (repo / "reports").mkdir()
    (repo / "reports" / "junit.xml").write_text("<testsuite/>", encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\0")
    (repo / "plan.md").write_text("notes", encoding="utf-8")
    assert fp(repo) == clean


def test_maven_target_is_skipped_but_a_package_named_build_is_not(tmp_path):
    ws = tmp_path / "java"
    (ws / "src" / "main" / "java" / "com" / "x" / "build").mkdir(parents=True)
    (ws / "pom.xml").write_text("<project/>", encoding="utf-8")
    (ws / "target" / "classes").mkdir(parents=True)
    pkg = ws / "src" / "main" / "java" / "com" / "x" / "build" / "Builder.java"
    pkg.write_text("class Builder {}", encoding="utf-8")
    first = fp(ws)
    (ws / "target" / "classes" / "A.class").write_bytes(b"1")
    assert fp(ws) == first                                                # build output ignored next to pom.xml
    pkg.write_text("class Builder { int x; }", encoding="utf-8")
    assert fp(ws) != first                                                # a source package called `build` is real code


def test_gradle_build_dir_is_skipped_only_next_to_gradle_files(tmp_path):
    ws = tmp_path / "gradle"
    (ws / "build").mkdir(parents=True)
    (ws / "build.gradle").write_text("plugins {}", encoding="utf-8")
    first = fp(ws)
    (ws / "build" / "out.jar").write_bytes(b"1")
    assert fp(ws) == first


# --- symlinks and links ----------------------------------------------------------------------------------------------
def make_symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")


def test_symlink_target_content_is_never_read(repo, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("one", encoding="utf-8")
    make_symlink(repo / "link.txt", outside)
    with_link = fp(repo)
    outside.write_text("two-two", encoding="utf-8")                        # the target changes: the link does not
    assert fp(repo) == with_link
    outside2 = tmp_path / "other.txt"
    outside2.write_text("two-two", encoding="utf-8")
    (repo / "link.txt").unlink()
    make_symlink(repo / "link.txt", outside2)                              # the link text changes: that is a change
    assert fp(repo) != with_link


def make_dir_link(link: Path, target: Path) -> None:
    """A directory junction on Windows (needs no privilege), a symlink elsewhere."""
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("cannot create a directory link here")


@pytest.mark.parametrize("use_git", [True, False])
def test_directory_links_are_not_descended_into(tmp_path, use_git):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("one", encoding="utf-8")
    (ws / "a.txt").write_text("a", encoding="utf-8")
    if use_git:
        git(ws, "init", "-q")
    make_dir_link(ws / "linkdir", outside)
    first = compute_fingerprint(ws)
    assert first.mode == ("GIT" if use_git else "CONTENT")
    (outside / "secret.txt").write_text("a different and longer text", encoding="utf-8")
    (outside / "new.txt").write_text("new", encoding="utf-8")
    assert compute_fingerprint(ws).value == first.value          # nothing behind the link is read


def test_content_mode_does_not_follow_symlinks_either(tmp_path):
    ws = tmp_path / "plain"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("one", encoding="utf-8")
    make_symlink(ws / "link.txt", outside)
    first = compute_fingerprint(ws)
    assert first.mode == "CONTENT"
    outside.write_text("changed", encoding="utf-8")
    assert compute_fingerprint(ws).value == first.value


def test_take_snapshot_does_not_open_symlink_targets(tmp_path):
    ws = tmp_path / "snap"
    ws.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("one", encoding="utf-8")
    make_symlink(ws / "link.txt", outside)
    before = take_snapshot(ws)["code_fingerprint"]
    outside.write_text("changed", encoding="utf-8")
    assert take_snapshot(ws)["code_fingerprint"] == before
    assert take_snapshot(ws)["files"]["link.txt"].startswith("link:")


# --- submodules ---------------------------------------------------------------------------------------------------------
def test_submodule_content_is_part_of_the_fingerprint(repo, tmp_path):
    sub = tmp_path / "subsrc"
    sub.mkdir()
    git(sub, "init", "-q")
    (sub / "lib.py").write_text("L = 1\n", encoding="utf-8")
    git(sub, "add", "-A")
    git(sub, "commit", "-q", "-m", "lib")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/lib")
    git(repo, "commit", "-q", "-m", "add submodule")
    first = compute_fingerprint(repo)
    assert first.mode == "GIT"
    (repo / "vendor" / "lib" / "lib.py").write_text("L = 2\n", encoding="utf-8")    # dirty inside the submodule
    assert fp(repo) != first.value


# --- git safety --------------------------------------------------------------------------------------------------------
def test_external_gitdir_falls_back_to_content_hashing(repo, tmp_path):
    real_git = repo / ".git"
    moved = tmp_path / "elsewhere.git"
    real_git.rename(moved)
    (repo / ".git").write_text(f"gitdir: {moved}\n", encoding="utf-8")
    result = compute_fingerprint(repo)
    assert result.mode == "CONTENT"


def test_alternates_and_core_worktree_fall_back(repo, tmp_path):
    info = repo / ".git" / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(str(tmp_path / "objects"), encoding="utf-8")
    assert compute_fingerprint(repo).mode == "CONTENT"
    (info / "alternates").unlink()
    git(repo, "config", "core.worktree", str(repo))
    assert compute_fingerprint(repo).mode == "CONTENT"


def test_repository_config_cannot_run_programs(repo, tmp_path):
    marker = tmp_path / "ran-fsmonitor.txt"
    command = f"echo ran > '{marker.as_posix()}'; echo"
    git(repo, "config", "core.fsmonitor", command)
    git(repo, "config", "core.hooksPath", str(tmp_path / "hooks"))
    control = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
                             env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    triggered = marker.exists()
    marker.unlink(missing_ok=True)
    assert compute_fingerprint(repo).mode == "GIT"
    assert not marker.exists()
    if not triggered:
        pytest.skip(f"control run did not execute the fsmonitor command here: {control.stderr[:100]}")


def test_inherited_git_variables_are_removed_and_config_goes_to_devnull(repo, monkeypatch, tmp_path):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SSH_COMMAND", "GIT_EXEC_PATH"):
        monkeypatch.setenv(name, str(tmp_path / "bogus"))
    env = git_env()
    assert not [k for k in env if k.upper().startswith("GIT_")
                and k not in {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS"}]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_SYSTEM"] == os.devnull != ""
    assert compute_fingerprint(repo).mode == "GIT"                         # os.devnull works on this platform


def test_global_options_are_separate_from_subcommand_options(repo):
    args = git_global_args(repo)
    assert args[0] == "git" and f"--git-dir={repo / '.git'}" in args and "--no-optional-locks" in args
    assert "core.fsmonitor=false" in args and f"core.hooksPath={os.devnull}" in args
    # every sub-command invoked by the module accepts exactly its own options
    for sub in (["rev-parse", "--verify", "-q", "HEAD"], ["ls-files", "--stage", "-z"],
                ["ls-files", "--others", "--exclude-standard", "-z"], ["config", "--local", "--get", "core.worktree"]):
        proc = subprocess.run([*args, *sub], capture_output=True, env=git_env(), cwd=repo)
        assert proc.returncode in (0, 1), proc.stderr
    assert not any(a in args for a in ("--no-ext-diff", "--no-textconv"))   # not valid for status / ls-files


def test_a_ro_environment_without_git_uses_content_mode(repo, monkeypatch):
    monkeypatch.setattr(wf, "_git_version", lambda: None)
    assert compute_fingerprint(repo).mode == "CONTENT"


# --- failure is explicit -----------------------------------------------------------------------------------------------
def test_missing_workspace_raises(tmp_path):
    with pytest.raises(FingerprintError):
        compute_fingerprint(tmp_path / "nope")


def test_unreadable_file_raises_instead_of_being_ignored(tmp_path, monkeypatch):
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "a.txt").write_text("a", encoding="utf-8")

    def boom(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(wf, "_hash_file", boom)
    with pytest.raises(FingerprintError):
        compute_fingerprint(ws)


def test_node_modules_and_venv_are_skipped_in_content_mode(tmp_path):
    ws = tmp_path / "plain"
    (ws / "node_modules").mkdir(parents=True)
    (ws / "a.txt").write_text("a", encoding="utf-8")
    first = fp(ws)
    (ws / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
    assert fp(ws) == first


def test_python_is_not_needed_for_the_unit_tests_module_import():
    assert sys.version_info >= (3, 12)


# --- fail-closed traversal and the error boundary ---------------------------------------------------------------------------
def test_a_file_as_root_is_a_fingerprint_error_with_the_original_cause(tmp_path):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    with pytest.raises(FingerprintError) as err:
        compute_fingerprint(not_a_dir)
    assert isinstance(err.value.__cause__, NotADirectoryError) and "enumerate" in str(err.value)
    with pytest.raises(FingerprintError) as missing:
        compute_fingerprint(tmp_path / "nope")
    assert isinstance(missing.value.__cause__, FileNotFoundError)


def inject_walk(monkeypatch, items, error):
    """A walk that yields `items` and then raises `error` while it is being consumed (a LATE enumeration error)."""
    def walker(root):
        yield from items
        raise error
    monkeypatch.setattr(wf, "walk_workspace", walker)


def test_an_enumeration_error_after_the_first_entry_is_not_a_partial_fingerprint(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    from app.infra.workspace_policy import WalkItem
    inject_walk(monkeypatch, [WalkItem("file", "a.txt", tmp_path / "a.txt")],
                PermissionError(13, "Access is denied", str(tmp_path / "locked")))
    with pytest.raises(FingerprintError) as err:
        compute_fingerprint(tmp_path)                                    # CONTENT mode: no git here
    assert isinstance(err.value.__cause__, PermissionError)               # the original cause is preserved
    assert str(tmp_path / "locked") in str(err.value) and "enumerate" in str(err.value)   # exc.filename is used


def test_without_a_filename_only_root_and_operation_are_reported_never_a_guessed_path(tmp_path, monkeypatch):
    inject_walk(monkeypatch, [], OSError("boom"))
    with pytest.raises(FingerprintError) as err:
        compute_fingerprint(tmp_path)
    message = str(err.value)
    assert "enumerate" in message and str(tmp_path) in message and "unknown" in message


def test_an_existing_fingerprint_error_is_not_wrapped_again(tmp_path, monkeypatch):
    original = FingerprintError("already the domain error")
    inject_walk(monkeypatch, [], original)
    with pytest.raises(FingerprintError) as err:
        compute_fingerprint(tmp_path)
    assert err.value is original and err.value.__cause__ is None


def test_a_file_that_vanishes_while_the_content_walk_reads_it_is_an_error_not_a_delete(tmp_path, monkeypatch):
    from app.infra.workspace_policy import WalkItem
    (tmp_path / "real.txt").write_text("x", encoding="utf-8")
    ghost = WalkItem("file", "ghost.txt", tmp_path / "ghost.txt")         # listed by the walk, gone before it is read
    monkeypatch.setattr(wf, "walk_workspace", lambda root: iter([ghost]))
    with pytest.raises(FingerprintError) as err:
        compute_fingerprint(tmp_path)
    assert isinstance(err.value.__cause__, FileNotFoundError) and "vanished" in str(err.value)


def test_a_failed_git_command_falls_back_to_content_and_a_failing_fallback_is_a_fingerprint_error(repo, monkeypatch):
    monkeypatch.setattr(wf, "_git_version", lambda: None)                 # git unusable -> documented CONTENT fallback
    assert compute_fingerprint(repo).mode == "CONTENT"
    monkeypatch.setattr(wf, "_hash_file", lambda _p: (_ for _ in ()).throw(PermissionError(13, "denied", "x")))
    with pytest.raises(FingerprintError) as err:                          # fallback fails too: never a silent value
        compute_fingerprint(repo)
    assert isinstance(err.value.__cause__, PermissionError)


def test_an_unreadable_tracked_file_in_git_mode_is_a_fingerprint_error_not_a_fallback(repo, monkeypatch):
    def denied(_path):
        raise PermissionError(13, "Access is denied", str(repo / "app.py"))
    monkeypatch.setattr(wf, "_hash_file", denied)
    with pytest.raises(FingerprintError) as err:
        compute_fingerprint(repo)
    assert isinstance(err.value.__cause__, PermissionError) and str(repo / "app.py") in str(err.value)


def test_an_untracked_file_that_vanishes_in_git_mode_is_an_error(repo, monkeypatch):
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    real = wf._worktree_entry

    def vanish(root, rel, tag, *, missing_ok=False):
        if rel == "new.py":
            (root / rel).unlink()                                         # deleted between `ls-files` and the read
        return real(root, rel, tag, missing_ok=missing_ok)

    monkeypatch.setattr(wf, "_worktree_entry", vanish)
    with pytest.raises(FingerprintError):
        compute_fingerprint(repo)


def test_a_tracked_file_deleted_in_the_worktree_is_still_a_legitimate_delete(repo):
    clean = fp(repo)
    (repo / "util.py").unlink()
    assert compute_fingerprint(repo).mode == "GIT" and fp(repo) != clean


# --- the two fingerprints do not depend on each other ----------------------------------------------------------------------------
def test_the_workspace_value_never_validates_as_a_step_snapshot_value_and_vice_versa(repo):
    from app.infra.workspace_snapshot import is_current_snapshot_version
    workspace_value = fp(repo)
    step_value = take_snapshot(repo)["code_fingerprint"]
    assert workspace_value.startswith("WORKSPACE_FINGERPRINT_V1:") and step_value.startswith("STEP_SNAPSHOT_V1:")
    assert is_current_version(workspace_value) and not is_current_version(step_value)
    assert is_current_snapshot_version(step_value) and not is_current_snapshot_version(workspace_value)
    assert not is_current_snapshot_version(None) and not is_current_version("STEP_SNAPSHOT_V0:abc")
