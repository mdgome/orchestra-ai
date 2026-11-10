"""`resolve_inside_root`: the project-root-relative resolver (WorkspaceGuard is anchored at WORKSPACE_ROOT)."""
import os

import pytest

from app.infra.workspace_guard import resolve_inside_root
from app.validation.exceptions import FailClosed


def test_a_plain_relative_path_resolves_below_the_root(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert resolve_inside_root(tmp_path, ".claude/settings.json") == (tmp_path / ".claude" / "settings.json").resolve()


@pytest.mark.parametrize("bad", ["", ".", "../x", "a/../../x", "a/.."])
def test_empty_and_parent_paths_are_rejected(tmp_path, bad):
    with pytest.raises(FailClosed):
        resolve_inside_root(tmp_path, bad)


def test_an_absolute_path_is_rejected(tmp_path):
    with pytest.raises(FailClosed):
        resolve_inside_root(tmp_path, str(tmp_path / "x"))


def _link(target, link):
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this user")


def test_a_symlink_component_is_rejected_even_when_it_points_inside(tmp_path):
    (tmp_path / "real").mkdir()
    _link(tmp_path / "real", tmp_path / "alias")
    with pytest.raises(FailClosed):
        resolve_inside_root(tmp_path, "alias/file.json")


def test_a_symlink_that_leaves_the_root_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    _link(outside, root / ".claude")
    with pytest.raises(FailClosed):
        resolve_inside_root(root, ".claude/settings.json")


def test_a_missing_path_is_still_resolved_so_callers_can_probe_it(tmp_path):
    assert not resolve_inside_root(tmp_path, ".venv/bin/python").exists()
