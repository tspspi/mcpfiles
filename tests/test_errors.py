import pytest

from mcpfiles.app.errors import ErrorInfo, map_exception
from mcpfiles.workspace import (
    ExtensionNotAllowedError,
    ImmutablePathError,
    PathOutsideWorkspace,
    QuotaExceededError,
)


def test_map_workspace_exceptions():
    info = map_exception(PathOutsideWorkspace("escape"))
    assert info.code == "path_out_of_bounds"

    immutable = map_exception(ImmutablePathError("locked"))
    assert immutable.code == "immutable_path"

    quota = map_exception(QuotaExceededError("too big"))
    assert quota.code == "quota_exceeded"


def test_map_extension_error():
    info = map_exception(ExtensionNotAllowedError("bad.py"))
    assert info.code == "extension_blocked"


def test_map_value_error_for_project():
    info = map_exception(ValueError("not a uuid"))
    assert info.code == "project_invalid"


def test_map_unknown_exception():
    info = map_exception(RuntimeError("boom"))
    assert info.code == "internal_error"
