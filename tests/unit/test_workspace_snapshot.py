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
