"""Error-code mapping helpers for MCP file operations."""

from __future__ import annotations

from dataclasses import dataclass

from mcpfiles.workspace import (
    ExtensionNotAllowedError,
    ImmutablePathError,
    MetadataAccessError,
    PathOutsideWorkspace,
    QuotaExceededError,
    WorkspaceError,
)


@dataclass
class ErrorInfo:
    code: str
    message: str


_EXCEPTION_CODE_MAP = {
    PathOutsideWorkspace: "path_out_of_bounds",
    MetadataAccessError: "permission_denied",
    ImmutablePathError: "immutable_path",
    ExtensionNotAllowedError: "extension_blocked",
    QuotaExceededError: "quota_exceeded",
    FileNotFoundError: "not_found",
    FileExistsError: "conflict",
    PermissionError: "permission_denied",
    IsADirectoryError: "conflict",
}


def map_exception(exc: Exception) -> ErrorInfo:
    """Return a canonical error code + message for the provided exception."""
    if isinstance(exc, WorkspaceError):
        code = _EXCEPTION_CODE_MAP.get(type(exc), "workspace_error")
        return ErrorInfo(code=code, message=str(exc))

    if isinstance(exc, ValueError):
        return ErrorInfo(code="project_invalid", message=str(exc))

    for exc_type, code in _EXCEPTION_CODE_MAP.items():
        if isinstance(exc, exc_type):
            return ErrorInfo(code=code, message=str(exc))

    return ErrorInfo(code="internal_error", message=str(exc))
