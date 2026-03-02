from pathlib import Path
from uuid import uuid4

import pytest

from mcpfiles.config.schema import APIKeyConfig
from mcpfiles.workspace import (
    ExtensionNotAllowedError,
    ImmutablePathError,
    MetadataAccessError,
    PathOutsideWorkspace,
    QuotaExceededError,
    WorkspaceManager,
)


def make_key_config(tmp_path, **overrides):
    data = {
        "id": "test",
        "kdf": {"algorithm": "argon2id", "salt": "c2FsdA==", "hash_len": 32},
        "root": str(tmp_path),
        "projects_enabled": False,
        "extension_whitelist": [".md", ".txt"],
        "allow_nonempty_delete": False,
        "immutable_paths": ["reference", "locked/file.txt"],
        "quota": {"soft_limit_bytes": None, "hard_limit_bytes": None},
        "logging": None,
        "mime_validation": False,
    }
    data.update(overrides)
    return APIKeyConfig(**data)


def test_resolve_path_blocks_escape(tmp_path):
    config = make_key_config(tmp_path)
    manager = WorkspaceManager(config)
    (tmp_path / "notes").mkdir()
    path = manager.resolve_path("notes/daily.md")
    assert path == (tmp_path / "notes" / "daily.md")
    with pytest.raises(PathOutsideWorkspace):
        manager.resolve_path("../outside.txt")


def test_metadata_access_blocked(tmp_path):
    config = make_key_config(tmp_path)
    manager = WorkspaceManager(config)
    with pytest.raises(MetadataAccessError):
        manager.resolve_path(".metadata/usage.json")


def test_project_root_resolution(tmp_path):
    project_id = str(uuid4())
    projects_dir = tmp_path / ".projects" / project_id
    projects_dir.mkdir(parents=True)
    config = make_key_config(tmp_path, projects_enabled=True)
    manager = WorkspaceManager(config, project_id=project_id)
    assert manager.root == projects_dir.resolve()

    with pytest.raises(ValueError):
        WorkspaceManager(config, project_id="not-a-uuid")

    with pytest.raises(FileNotFoundError):
        WorkspaceManager(config, project_id=str(uuid4()))


def test_immutable_paths_enforced(tmp_path):
    (tmp_path / "reference").mkdir()
    config = make_key_config(tmp_path)
    manager = WorkspaceManager(config)
    mutable = manager.resolve_path("notes/ok.md")
    manager.ensure_mutable(mutable)
    immutable_file = manager.resolve_path("reference/data.md")
    with pytest.raises(ImmutablePathError):
        manager.ensure_mutable(immutable_file)


def test_extension_whitelist(tmp_path):
    config = make_key_config(tmp_path)
    manager = WorkspaceManager(config)
    allowed = manager.resolve_path("notes/day.md")
    assert manager.extension_allowed(allowed)
    disallowed = manager.resolve_path("notes/script.py")
    with pytest.raises(ExtensionNotAllowedError):
        manager.enforce_extension(disallowed)


def test_mime_validation(tmp_path):
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hello world", encoding="utf-8")
    config = make_key_config(tmp_path, mime_validation=True)
    manager = WorkspaceManager(config)
    resolved = manager.resolve_path("notes.txt")
    assert manager.validate_mime(resolved)  # text should be allowed


def test_usage_delta_and_recalc(tmp_path):
    config = make_key_config(tmp_path)
    manager = WorkspaceManager(config)
    manager.record_usage_delta(200)
    assert manager.get_usage() == 200
    manager.record_usage_delta(-50)
    assert manager.get_usage() == 150

    # create real file and ensure recalc uses actual disk values
    file_path = tmp_path / "files" / "note.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("data", encoding="utf-8")

    metadata_file = manager.metadata_usage_path().parent / "usage.log"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text("should be ignored", encoding="utf-8")

    total = manager.recalculate_usage()
    assert total == file_path.stat().st_size
    assert manager.get_usage() == total


def test_quota_enforcement(tmp_path):
    config = make_key_config(
        tmp_path,
        quota={"soft_limit_bytes": 50, "hard_limit_bytes": 60},
    )
    manager = WorkspaceManager(config)
    manager.record_usage_delta(40)
    manager.enforce_quota(10)  # should be fine (projected 50)
    with pytest.raises(QuotaExceededError):
        manager.enforce_quota(25)
