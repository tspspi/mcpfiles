import json
from textwrap import dedent
from uuid import uuid4

import pytest

from mcpfiles.config.schema import APIKeyConfig
from mcpfiles.fs_operations import FileOperations
from mcpfiles.workspace import MetadataAccessError, QuotaExceededError, WorkspaceManager


def key_config(tmp_path, **overrides):
    base = {
        "id": "test",
        "kdf": {"algorithm": "argon2id", "salt": "c2FsdA==", "hash_len": 32},
        "root": str(tmp_path),
        "projects_enabled": True,
        "extension_whitelist": [".md", ".txt"],
        "allow_nonempty_delete": False,
        "immutable_paths": ["reference"],
        "quota": {"soft_limit_bytes": None, "hard_limit_bytes": None},
        "logging": None,
        "mime_validation": False,
    }
    base.update(overrides)
    return APIKeyConfig(**base)


def manager_and_ops(tmp_path, **overrides):
    cfg = key_config(tmp_path, **overrides)
    workspace = WorkspaceManager(cfg)
    ops = FileOperations(workspace)
    return workspace, ops


def test_write_read_and_delete_file(tmp_path):
    _, ops = manager_and_ops(tmp_path)
    result = ops.write_file("notes/day.md", "hello world")
    assert result["size"] == len("hello world".encode())

    read = ops.read_file("notes/day.md")
    assert read["content"] == "hello world"

    listing = ops.list_directory("notes")
    assert listing and listing[0]["path"].endswith("day.md")

    removal = ops.delete_file("notes/day.md")
    assert removal["removed"] is True


def test_create_remove_directory(tmp_path):
    cfg = key_config(tmp_path, allow_nonempty_delete=True)
    workspace = WorkspaceManager(cfg)
    ops = FileOperations(workspace)
    ops.create_directory("a/b/c")
    ops.write_file("a/b/c/file.md", "data")
    resp = ops.remove_directory("a", recursive=True)
    assert resp["removed"] is True


def test_remove_directory_without_recursive(tmp_path):
    _, ops = manager_and_ops(tmp_path)
    ops.create_directory("dir")
    ops.write_file("dir/file.md", "data")
    with pytest.raises(PermissionError):
        ops.remove_directory("dir", recursive=False)


def test_metadata_and_stat(tmp_path):
    workspace, ops = manager_and_ops(tmp_path)
    ops.write_file("info/file.md", "data")
    meta = ops.get_metadata("info/file.md")
    assert meta["size"] == len("data")

    stat = ops.stat_tree("info")
    assert stat["file_count"] == 1
    assert stat["total_bytes"] == len("data")


def test_quota_enforcement_during_write(tmp_path):
    cfg = key_config(tmp_path, quota={"soft_limit_bytes": 5, "hard_limit_bytes": 6})
    workspace = WorkspaceManager(cfg)
    ops = FileOperations(workspace)
    ops.write_file("a.md", "12345")
    with pytest.raises(QuotaExceededError):
        ops.write_file("b.md", "1234")


def test_project_listing_and_creation(tmp_path):
    workspace, ops = manager_and_ops(tmp_path)
    project_id = str(uuid4())
    ops.create_project(project_id)
    projects = ops.list_projects()
    ids = [entry["id"] for entry in projects]
    assert project_id in ids


def test_metadata_access_blocked_under_root(tmp_path):
    workspace, ops = manager_and_ops(tmp_path)
    usage_path = workspace.metadata_usage_path()
    usage_path.write_text("{}", encoding="utf-8")
    with pytest.raises(MetadataAccessError):
        ops.read_file(".metadata/usage.json")


def test_metadata_access_blocked_inside_project_dirs(tmp_path):
    workspace, ops = manager_and_ops(tmp_path)
    project_id = str(uuid4())
    project_meta = workspace.base_root / ".projects" / project_id / ".metadata"
    project_meta.mkdir(parents=True, exist_ok=True)
    usage_path = project_meta / "usage.json"
    usage_path.write_text("{}", encoding="utf-8")
    with pytest.raises(MetadataAccessError):
        ops.delete_file(f".projects/{project_id}/.metadata/usage.json")


def test_apply_patch_codex_format(tmp_path):
    _, ops = manager_and_ops(tmp_path)
    ops.write_file("notes/day.md", "hello world\nsecond line\n")
    patch = dedent(
        """\
        *** Begin Patch
        *** Update File: notes/day.md
        @@ -1,2 +1,2 @@
        -hello world
        +hello codex
        second line
        *** End Patch
        """
    )
    result = ops.apply_patch(None, patch)
    assert result["updated"] == 1
    updated = ops.read_file("notes/day.md")["content"]
    assert "hello codex" in updated


def test_apply_patch_plain_unified_diff(tmp_path):
    _, ops = manager_and_ops(tmp_path)
    ops.write_file("notes/day.md", "alpha\nbeta\n")
    diff = dedent(
        """\
        @@ -1,2 +1,3 @@
        -alpha
        +ALPHA
        beta
        +gamma
        """
    )
    result = ops.apply_patch("notes/day.md", diff)
    assert result["updated"] == 1
    updated = ops.read_file("notes/day.md")["content"]
    assert updated.startswith("ALPHA")
    assert updated.strip().endswith("gamma")


def test_apply_patch_add_and_delete(tmp_path):
    _, ops = manager_and_ops(tmp_path)
    add_patch = dedent(
        """\
        *** Begin Patch
        *** Add File: notes/new.md
        +created via patch
        *** End Patch
        """
    )
    add_result = ops.apply_patch(None, add_patch)
    assert add_result["added"] == 1
    assert "created via patch" in ops.read_file("notes/new.md")["content"]

    delete_patch = dedent(
        """\
        *** Begin Patch
        *** Delete File: notes/new.md
        *** End Patch
        """
    )
    delete_result = ops.apply_patch(None, delete_patch)
    assert delete_result["deleted"] == 1
    with pytest.raises(FileNotFoundError):
        ops.read_file("notes/new.md")


def test_apply_patch_validation_failure(tmp_path):
    _, ops = manager_and_ops(tmp_path)
    ops.write_file("notes/day.md", "original line\n")
    bad_patch = dedent(
        """\
        *** Begin Patch
        *** Update File: notes/day.md
        @@ -1,1 +1,1 @@
        -does not exist
        +new line
        *** End Patch
        """
    )
    with pytest.raises(ValueError):
        ops.apply_patch(None, bad_patch)
