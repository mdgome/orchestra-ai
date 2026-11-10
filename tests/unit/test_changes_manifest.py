"""CHANGES_MANIFEST_V1: scope per mode, entry kinds, the stored encoding and its bounds, dirty paths, comparison, and the
cooperative deadline. Real git is used where git decides the answer."""
import gzip
import hashlib
import json
import os
import subprocess
import zlib

import pytest

from app.infra import changes_manifest as CM
from app.infra import workspace_fingerprint as WF
from app.infra.deadline import Deadline, DeadlineExceeded
from app.validation import policies as P

SHA = "a" * 64
OID = "b" * 40


def git(root, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", *args], cwd=root,
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.py").write_text("print(1)\n")
    (root / "b.txt").write_text("b\n")
    (root / ".gitignore").write_text("out/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    return root


def encode_decode(measured):
    enc = CM.encode(measured)
    return enc, CM.decode(enc.data, version=CM.MANIFEST_VERSION, expected_mode=measured.mode, expected_sha256=enc.sha256,
                          expected_bytes=enc.uncompressed_bytes)


# --- scope by mode --------------------------------------------------------------------------------------------------------
def test_git_mode_covers_tracked_and_untracked_non_ignored_files_but_not_ignored_output(repo):
    (repo / "out").mkdir()
    (repo / "out" / "build.o").write_text("object")
    (repo / "new.txt").write_text("new")
    (repo / "a.py").write_text("print(2)\n")
    measured = CM.measure(repo)
    assert measured.mode == "GIT" and measured.head_kind == "COMMIT" and len(measured.head_sha) in (40, 64)
    assert set(measured.entries) == {".gitignore", "a.py", "b.txt", "new.txt"}          # out/build.o is ignored output
    assert measured.dirty_paths == ("a.py", "new.txt")                                  # what already differed from HEAD
    assert measured.entries["a.py"]["sha256"] == hashlib.sha256((repo / "a.py").read_bytes()).hexdigest()   # real disk bytes
    assert all(e["kind"] == "FILE" for e in measured.entries.values())


def test_a_tracked_file_deleted_in_the_working_tree_has_no_entry_but_is_dirty(repo):
    (repo / "b.txt").unlink()
    measured = CM.measure(repo)
    assert "b.txt" not in measured.entries and "b.txt" in measured.dirty_paths


def test_an_unborn_head_is_a_valid_git_baseline(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    (root / "x.txt").write_text("x")
    measured = CM.measure(root)
    assert (measured.mode, measured.head_kind, measured.head_sha) == ("GIT", "UNBORN", None)
    assert set(measured.entries) == {"x.txt"} and measured.dirty_paths == ("x.txt",)


def test_content_mode_walks_a_non_git_workspace_with_the_workspace_policy(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("m")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("skipped")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "junit.xml").write_text("volatile")
    measured = CM.measure(tmp_path)
    assert (measured.mode, measured.head_kind, measured.head_sha, measured.dirty_paths) == ("CONTENT", None, None, ())
    assert set(measured.entries) == {"src/m.py"}


def test_a_forced_mode_is_honoured_or_refused(repo, tmp_path):
    assert CM.measure(repo, mode="CONTENT").mode == "CONTENT"          # the same mode as the baseline, for a like-for-like diff
    assert not any(p == ".git" or p.startswith(".git/") for p in CM.measure(repo, mode="CONTENT").entries)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(CM.ManifestFailure) as info:
        CM.measure(plain, mode="GIT")
    assert info.value.reason == "MODE_UNAVAILABLE"


def test_a_gitlink_is_recorded_by_commit_id_only(repo):
    inner = repo / "vendor" / "lib"
    inner.mkdir(parents=True)
    git(inner, "init", "-q")
    (inner / "f.txt").write_text("inner")
    git(inner, "add", ".")
    git(inner, "commit", "-qm", "inner")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=inner, capture_output=True, text=True).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},vendor/lib")
    measured = CM.measure(repo)
    assert measured.entries["vendor/lib"] == {"kind": "GITLINK", "oid": sha}
    assert not any(p.startswith("vendor/lib/") for p in measured.entries)           # the inner content is not described


def test_a_symlink_is_described_by_its_target_string_and_never_opened(repo):
    try:
        os.symlink("../outside/secret.txt", repo / "link")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this user")
    measured = CM.measure(repo)
    assert measured.entries["link"] == {"kind": "SYMLINK", "target": "../outside/secret.txt"}


@pytest.mark.skipif(os.name == "nt", reason="the executable bit does not exist on Windows (same policy as the fingerprint)")
def test_executable_follows_the_working_tree_mode_not_the_index(repo):
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\n")
    git(repo, "add", "run.sh")
    git(repo, "commit", "-qm", "script")
    assert CM.measure(repo).entries["run.sh"]["executable"] is False
    script.chmod(0o755)                                                              # unstaged chmod
    assert CM.measure(repo).entries["run.sh"]["executable"] is True
    before = CM.measure(repo).entries
    git(repo, "update-index", "--chmod=-x", "run.sh")                                # staged-only: working tree unchanged
    assert CM.measure(repo).entries == before


def test_on_windows_no_file_is_ever_executable(repo):
    if os.name != "nt":
        pytest.skip("a Windows-only policy")
    assert all(not e["executable"] for e in CM.measure(repo).entries.values() if e["kind"] == "FILE")


def test_a_chmod_of_a_tracked_file_is_a_modification_when_the_bit_exists(repo):
    if os.name == "nt":
        pytest.skip("no executable bit on Windows")
    base = CM.measure(repo).entries
    (repo / "a.py").chmod(0o755)
    changes = CM.compute_changes(base, CM.measure(repo).entries)
    assert [(c.path, c.change_type) for c in changes] == [("a.py", "MODIFIED")]


# --- encoding -------------------------------------------------------------------------------------------------------------------
def sample(mode="CONTENT", **entries):
    return CM.Measured(mode, None, None, entries or {"f": {"kind": "FILE", "sha256": SHA, "executable": False}}, ())


def test_every_entry_kind_round_trips_through_the_stored_form():
    measured = sample(
        **{"src/app.py": {"kind": "FILE", "sha256": SHA, "executable": False},
           "bin/run": {"kind": "FILE", "sha256": "c" * 64, "executable": True},
           "docs/latest": {"kind": "SYMLINK", "target": "../shared/readme.md"},
           "vendor/lib": {"kind": "GITLINK", "oid": "d" * 64}})
    enc, back = encode_decode(measured)
    assert back == measured.entries and enc.file_count == 4 and len(enc.sha256) == 64


def test_the_digest_is_of_the_uncompressed_canonical_json_and_is_order_independent():
    a = sample(**{"b": {"kind": "FILE", "sha256": SHA, "executable": False}, "a": {"kind": "SYMLINK", "target": "t"}})
    b = sample(**{"a": {"kind": "SYMLINK", "target": "t"}, "b": {"kind": "FILE", "sha256": SHA, "executable": False}})
    ea, eb = CM.encode(a), CM.encode(b)
    raw = gzip.decompress(ea.data)
    assert ea.sha256 == eb.sha256 == hashlib.sha256(raw).hexdigest() and ea.uncompressed_bytes == len(raw)
    assert raw == b'{"entries":{"a":{"kind":"SYMLINK","target":"t"},"b":{"executable":false,"kind":"FILE","sha256":"' + SHA.encode() \
        + b'"}},"mode":"CONTENT","schema":"CHANGES_MANIFEST_V1"}'


@pytest.mark.parametrize("entry", [
    {"kind": "FILE", "sha256": SHA}, {"kind": "FILE", "sha256": SHA, "executable": 1},
    {"kind": "FILE", "sha256": SHA.upper(), "executable": False}, {"kind": "FILE", "sha256": "a" * 63, "executable": False},
    {"kind": "FILE", "sha256": SHA, "executable": False, "mode": "100644"},
    {"kind": "SYMLINK"}, {"kind": "SYMLINK", "target": ""}, {"kind": "SYMLINK", "target": "a\x00b"},
    {"kind": "SYMLINK", "target": "t", "extra": 1}, {"kind": "SYMLINK", "target": "x" * (P.MANIFEST_MAX_SYMLINK_TARGET_LEN + 1)},
    {"kind": "GITLINK", "oid": "z" * 40}, {"kind": "GITLINK", "oid": "b" * 41}, {"kind": "GITLINK"},
    {"kind": "DIRECTORY"}, {"sha256": SHA}, "FILE", None])
def test_a_malformed_entry_is_a_corrupt_manifest(entry):
    with pytest.raises(CM.ManifestFailure) as info:
        CM.check_entry(entry)
    assert info.value.reason == "MANIFEST_CORRUPT"


def raw_manifest(document: dict) -> tuple[bytes, str, int]:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest(), len(raw)


def read(data, sha, size, *, version="CHANGES_MANIFEST_V1", mode="CONTENT"):
    return CM.decode(data, version=version, expected_mode=mode, expected_sha256=sha, expected_bytes=size)


def reason(fn, *a, **k):
    with pytest.raises(CM.ManifestFailure) as info:
        fn(*a, **k)
    return info.value.reason


def test_a_damaged_stored_manifest_is_reported_not_raised_as_a_server_error():
    good = {"schema": "CHANGES_MANIFEST_V1", "mode": "CONTENT", "entries": {"f": {"kind": "FILE", "sha256": SHA, "executable": False}}}
    data, sha, size = raw_manifest(good)
    assert read(data, sha, size) == good["entries"]
    assert reason(read, data[:-8], sha, size) == "MANIFEST_CORRUPT"                       # truncated gzip
    assert reason(read, b"not gzip at all", sha, size) == "MANIFEST_CORRUPT"
    assert reason(read, data, "0" * 64, size) == "MANIFEST_CORRUPT"                       # digest mismatch
    assert reason(read, data, sha, size - 1) in ("MANIFEST_TOO_LARGE", "MANIFEST_CORRUPT")
    assert reason(read, data, sha, size, version="CHANGES_MANIFEST_V9") == "UNSUPPORTED_MANIFEST_VERSION"
    assert reason(read, data, sha, size, mode="GIT") == "MANIFEST_CORRUPT"                # mode must match the baseline row
    assert reason(read, data + b"junk", sha, size) == "MANIFEST_CORRUPT"                  # trailing data


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=1), lambda d: d.pop("mode"), lambda d: d.update(schema="OTHER"),
    lambda d: d.update(entries=[]), lambda d: d["entries"].update({"../x": {"kind": "SYMLINK", "target": "t"}}),
    lambda d: d["entries"].update({"/abs": {"kind": "SYMLINK", "target": "t"}}),
    lambda d: d["entries"].update({"a/../b": {"kind": "SYMLINK", "target": "t"}}),
    lambda d: d["entries"].update({"f": {"kind": "WEIRD"}})])
def test_a_manifest_with_a_wrong_shape_is_corrupt(mutate):
    doc = {"schema": "CHANGES_MANIFEST_V1", "mode": "CONTENT", "entries": {"f": {"kind": "FILE", "sha256": SHA, "executable": False}}}
    mutate(doc)
    data, sha, size = raw_manifest(doc)
    assert reason(read, data, sha, size) == "MANIFEST_CORRUPT"


def test_a_compression_bomb_is_stopped_before_it_fills_memory():
    big = b"A" * (20 * 1024 * 1024)
    bomb = gzip.compress(big, mtime=0)
    assert len(bomb) < 100_000                                             # tiny on disk, huge when expanded
    assert reason(read, bomb, hashlib.sha256(big).hexdigest(), 1000) == "MANIFEST_TOO_LARGE"      # lies about its size
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    assert len(stream.decompress(bomb, 1001)) == 1001 and stream.unconsumed_tail                   # the cap is what bounds it


def test_the_limits_are_enforced_on_write_at_their_boundaries(monkeypatch):
    entries = {f"f{i}": {"kind": "SYMLINK", "target": "t"} for i in range(5)}
    monkeypatch.setattr(P, "MANIFEST_MAX_FILES", 5)
    assert CM.encode(sample(**entries)).file_count == 5                    # exactly the limit passes
    monkeypatch.setattr(P, "MANIFEST_MAX_FILES", 4)
    assert reason(CM.encode, sample(**entries)) == "TOO_MANY_FILES"
    monkeypatch.setattr(P, "MANIFEST_MAX_FILES", 100)
    size = CM.encode(sample(**entries)).uncompressed_bytes
    monkeypatch.setattr(P, "MANIFEST_MAX_UNCOMPRESSED_BYTES", size)
    CM.encode(sample(**entries))
    monkeypatch.setattr(P, "MANIFEST_MAX_UNCOMPRESSED_BYTES", size - 1)
    assert reason(CM.encode, sample(**entries)) == "MANIFEST_TOO_LARGE"
    monkeypatch.setattr(P, "MANIFEST_MAX_UNCOMPRESSED_BYTES", 10**9)
    monkeypatch.setattr(P, "MANIFEST_MAX_COMPRESSED_BYTES", 10)
    assert reason(CM.encode, sample(**entries)) == "MANIFEST_TOO_LARGE"


def test_decode_refuses_a_stored_manifest_above_the_policy_maximum(monkeypatch):
    monkeypatch.setattr(P, "MANIFEST_MAX_UNCOMPRESSED_BYTES", 100)
    assert reason(read, b"x", "0" * 64, 101) == "MANIFEST_TOO_LARGE"


# --- dirty paths ----------------------------------------------------------------------------------------------------------------
def test_dirty_paths_are_unique_sorted_and_safe():
    assert CM.normalize_dirty_paths(["b/x", "a", "b/x"]) == ["a", "b/x"]


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../b", "a\x00b", "a\nb", "C:/x", "a\\b", "", "a//b", "./a",
                                 "caf\udce9", "/"])
def test_unsafe_dirty_paths_make_the_whole_baseline_unusable(bad):
    assert reason(CM.normalize_dirty_paths, ["fine", bad]) == "DIRTY_PATHS_INVALID"


def test_a_non_utf8_name_is_a_reason_code_not_a_database_error_later():
    # git hands such a name over as surrogate-escaped text; jsonb cannot store it, so it must stop here, as a failure
    assert reason(CM.safe_rel_path, "caf\udce9.txt", max_len=255, reason="INVALID_PATH") == "INVALID_PATH"


def test_an_untracked_nested_git_repository_is_a_directory_not_an_invalid_path(repo):
    nested = repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "x.txt").write_text("x")
    (repo / "plain.txt").write_text("p")
    measured = CM.measure(repo)                                   # used to fail as INVALID_PATH / DIRTY_PATHS_INVALID
    assert "plain.txt" in measured.entries and not any(p.startswith("vendor/") for p in measured.entries)
    assert "vendor/lib" in measured.dirty_paths and "plain.txt" in measured.dirty_paths
    assert CM.normalize_dirty_paths(["vendor/lib/"]) == ["vendor/lib"]


def test_the_link_ancestor_check_is_done_once_per_directory(repo, monkeypatch):
    for i in range(5):
        (repo / "deep" / "dir").mkdir(parents=True, exist_ok=True)
        (repo / "deep" / "dir" / f"f{i}.txt").write_text(str(i))
    calls = []
    real = CM.is_link_or_junction
    monkeypatch.setattr(CM, "is_link_or_junction", lambda p: (calls.append(p), real(p))[1])
    CM.measure(repo)
    assert sum(1 for p in calls if p.name == "deep") == 1 and sum(1 for p in calls if p.name == "dir") == 1


def test_dirty_path_limits_fail_instead_of_truncating(monkeypatch):
    monkeypatch.setattr(P, "DIRTY_PATHS_MAX_ITEMS", 3)
    assert len(CM.normalize_dirty_paths(["a", "b", "c"])) == 3                           # the boundary passes
    assert reason(CM.normalize_dirty_paths, ["a", "b", "c", "d"]) == "DIRTY_PATHS_LIMIT"
    monkeypatch.setattr(P, "DIRTY_PATHS_MAX_ITEMS", 100)
    monkeypatch.setattr(P, "DIRTY_PATHS_MAX_PATH_LEN", 5)
    assert CM.normalize_dirty_paths(["abcde"]) == ["abcde"]
    assert reason(CM.normalize_dirty_paths, ["abcdef"]) == "DIRTY_PATHS_INVALID"
    monkeypatch.setattr(P, "DIRTY_PATHS_MAX_PATH_LEN", 1024)
    monkeypatch.setattr(P, "DIRTY_PATHS_MAX_SERIALIZED_BYTES", 20)
    assert reason(CM.normalize_dirty_paths, ["aaaaaaaaaa", "bbbbbbbbbb"]) == "DIRTY_PATHS_LIMIT"


def test_too_many_dirty_files_make_the_git_measurement_fail_as_a_whole(repo, monkeypatch):
    for i in range(6):
        (repo / f"untracked{i}.txt").write_text("u")
    monkeypatch.setattr(P, "DIRTY_PATHS_MAX_ITEMS", 5)
    assert reason(CM.measure, repo) == "DIRTY_PATHS_LIMIT"


# --- comparison -----------------------------------------------------------------------------------------------------------------
def f(sha, x=False):
    return {"kind": "FILE", "sha256": sha, "executable": x}


def test_changes_are_added_removed_modified_and_renamed_by_content_hash():
    base = {"keep": f("1" * 64), "gone": f("2" * 64), "edit": f("3" * 64), "old/name": f("4" * 64), "link": {"kind": "SYMLINK", "target": "a"}}
    now = {"keep": f("1" * 64), "edit": f("9" * 64), "new/name": f("4" * 64), "fresh": f("5" * 64), "link": {"kind": "SYMLINK", "target": "b"}}
    got = {c.path: (c.change_type, c.previous_path) for c in CM.compute_changes(base, now)}
    assert got == {"edit": ("MODIFIED", None), "fresh": ("ADDED", None), "gone": ("REMOVED", None),
                   "link": ("MODIFIED", None), "new/name": ("RENAMED", "old/name")}


def test_two_identical_removed_files_pair_one_to_one_with_added_ones():
    base = {"a": f("1" * 64), "b": f("1" * 64)}
    now = {"c": f("1" * 64)}
    types = sorted((c.path, c.change_type) for c in CM.compute_changes(base, now))
    assert types == [("b", "REMOVED"), ("c", "RENAMED")]


def test_a_path_that_was_dirty_at_the_start_is_flagged_not_claimed():
    base, now = {"a": f("1" * 64), "b": f("2" * 64)}, {"a": f("9" * 64), "b": f("8" * 64)}
    flags = {c.path: c.pre_existing_dirty for c in CM.compute_changes(base, now, frozenset({"a"}))}
    assert flags == {"a": True, "b": False}


def test_no_difference_means_no_change_even_for_a_dirty_start():
    base = {"a": f("1" * 64)}
    assert CM.compute_changes(base, dict(base), frozenset({"a"})) == []


# --- sensitive paths --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path,sensitive", [
    (".env", True), (".env.production", True), ("config/.ENV", True), ("certs/server.pem", True), ("keys/id_rsa", True),
    ("keys/id_rsa.pub", True), ("aws/credentials", True), ("app/secrets.yml", True), ("a.keystore", True),
    ("src/app.py", False), ("docs/env.md", False), ("environment.txt", False)])
def test_sensitive_paths_are_recognised_by_name(path, sensitive):
    assert CM.is_sensitive_path(path) is sensitive


# --- the cooperative deadline --------------------------------------------------------------------------------------------------------
def test_an_expired_deadline_stops_measuring_between_entries(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("x")
    clock = iter(range(1000))
    deadline = Deadline(5, clock=lambda: next(clock))          # every look at the clock costs one "second"
    with pytest.raises(DeadlineExceeded):
        CM.measure(tmp_path, deadline=deadline)


def test_an_expired_deadline_stops_hashing_between_chunks(tmp_path, monkeypatch):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (3 * (1 << 20)))
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        return float(ticks["n"])

    with pytest.raises(DeadlineExceeded):
        CM.measure(tmp_path, deadline=Deadline(3, clock=clock))


def test_without_a_deadline_the_walk_is_unbounded_as_before(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    assert set(CM.measure(tmp_path).entries) == {"f.txt"}


def test_git_gets_min_of_its_default_and_the_remaining_time_and_an_expired_deadline_means_no_git_at_all(repo, monkeypatch):
    seen = []

    def fake_git(root, subcommand, *, ok_codes=(0,), timeout=None):
        seen.append(timeout)
        return b""

    monkeypatch.setattr(WF, "_git", fake_git)
    WF._bounded_git(repo, ["status"], Deadline(5))
    assert seen[-1] is not None and 0 < seen[-1] <= 5
    WF._bounded_git(repo, ["status"], Deadline(10_000))
    assert seen[-1] == WF.GIT_TIMEOUT_SECONDS                              # never longer than the original default
    WF._bounded_git(repo, ["status"], None)
    assert seen[-1] is None                                                 # no deadline: the original behaviour
    calls = len(seen)
    with pytest.raises(DeadlineExceeded):
        WF._bounded_git(repo, ["status"], Deadline(-1))
    assert len(seen) == calls                                               # git was not even started


def test_a_git_timeout_after_the_deadline_is_a_deadline_not_a_git_failure(repo, monkeypatch):
    clock = {"t": 0.0}
    deadline = Deadline(1, clock=lambda: clock["t"])

    def slow_git(root, subcommand, *, ok_codes=(0,), timeout=None):
        clock["t"] = 5.0                                                    # the time ran out while git ran
        raise WF._GitUnusable("git status failed to run: timed out")

    monkeypatch.setattr(WF, "_git", slow_git)
    with pytest.raises(DeadlineExceeded):
        WF._bounded_git(repo, ["status"], deadline)
    with pytest.raises(WF.GitInventoryError):                               # a failure within the time is a git failure
        WF._bounded_git(repo, ["status"], Deadline(100))
