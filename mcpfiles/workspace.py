"""Workspace manager enforcing filesystem constraints."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from uuid import UUID

from mcpfiles.config.schema import APIKeyConfig

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Base error for workspace operations."""


class PathOutsideWorkspace(WorkspaceError):
    """Raised when a user tries to escape the sandbox root."""


class MetadataAccessError(WorkspaceError):
    """Raised when the `.metadata` directory is accessed directly."""


class ImmutablePathError(WorkspaceError):
    """Raised when trying to modify an immutable path."""


class ExtensionNotAllowedError(WorkspaceError):
    """Raised when writing files with disallowed extensions."""


class QuotaExceededError(WorkspaceError):
    """Raised when an operation would exceed the configured hard limit."""


@dataclass
class WorkspaceManager:
    """Manage a single API key (and optional project) workspace."""

    key_config: APIKeyConfig
    project_id: Optional[str] = None
    metadata_dirname: str = ".metadata"
    file_command: str = "file"

    def __post_init__(self):
        self.base_root = Path(self.key_config.root).resolve()
        if not self.base_root.exists():
            self.base_root.mkdir(parents=True, exist_ok=True)
        self.root = self._resolve_project_root()
        self.metadata_root = self.root / self.metadata_dirname

    # ------------------------------------------------------------------ helpers
    def _resolve_project_root(self) -> Path:
        if not self.project_id:
            return self.base_root
        if not self.key_config.projects_enabled:
            raise ValueError("Projects are disabled for this API key")
        try:
            UUID(self.project_id)
        except ValueError as exc:
            raise ValueError("project_id must be a valid UUID") from exc
        project_path = self.base_root / ".projects" / self.project_id
        if not project_path.exists():
            raise FileNotFoundError(f"Project directory {project_path} does not exist")
        return project_path.resolve()

    def _contains_metadata(self, path: Path) -> bool:
        try:
            relative = path.relative_to(self.base_root)
        except ValueError:
            return False
        return self.metadata_dirname in relative.parts

    def _ensure_within_root(self, path: Path):
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise PathOutsideWorkspace(f"{path} escapes workspace root {self.root}") from exc

    def resolve_path(self, relative_path: str, *, allow_metadata: bool = False) -> Path:
        """Resolve a relative path under the sandbox root."""
        candidate = (self.root / relative_path).resolve()
        self._ensure_within_root(candidate)
        if not allow_metadata and self._contains_metadata(candidate):
            raise MetadataAccessError("Direct access to .metadata is forbidden")
        return candidate

    # ---------------------------------------------------------------- immutables
    def ensure_mutable(self, path: Path):
        """Raise if `path` falls under an immutable directory/file."""
        rel = path.relative_to(self.root)
        segments = rel.parts
        immutable_paths = [Path(p) for p in self.key_config.immutable_paths]
        for immutable in immutable_paths:
            imm_parts = immutable.parts
            if segments[: len(imm_parts)] == imm_parts:
                raise ImmutablePathError(f"{rel} is immutable")

    # --------------------------------------------------------------- extensions
    def extension_allowed(self, path: Path) -> bool:
        """Check if the suffix is in the whitelist (case-insensitive)."""
        whitelist = self.key_config.extension_whitelist
        if not whitelist:
            return True
        suffix = path.suffix.lower()
        normalized = [entry.lower() for entry in whitelist]
        return suffix in normalized

    def enforce_extension(self, path: Path):
        if not self.extension_allowed(path):
            raise ExtensionNotAllowedError(f"{path.name} lacks an allowed extension")

    # --------------------------------------------------------------- MIME check
    def validate_mime(self, path: Path) -> bool:
        """Validate MIME type using the `file` utility, if enabled."""
        if not self.key_config.mime_validation:
            return True
        try:
            result = subprocess.run(
                [self.file_command, "--brief", "--mime-type", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            logger.warning("file utility not found; skipping MIME validation")
            return True

        if result.returncode != 0:
            logger.warning("file utility failed (%s); skipping MIME validation", result.returncode)
            return True

        mime = result.stdout.strip()
        allowed_prefixes = ("text/", "application/json", "application/xml")
        valid = mime.startswith(allowed_prefixes)
        if not valid:
            logger.warning("Blocked MIME type %s for %s", mime, path)
        return valid

    # ----------------------------------------------------------- metadata files
    def metadata_usage_path(self) -> Path:
        self.metadata_root.mkdir(parents=True, exist_ok=True)
        return self.metadata_root / "usage.json"

    # ----------------------------------------------------------------- logging
    def relative_path(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    # ------------------------------------------------------------ usage/quota
    def _default_usage_payload(self) -> dict:
        return {
            "root": str(self.root),
            "project_id": self.project_id,
            "used_bytes": 0,
            "last_full_scan": None,
            "last_delta_update": None,
        }

    @contextmanager
    def _locked_usage_file(self, mode: str):
        path = self.metadata_usage_path()
        # Ensure file exists for read/update paths
        if "r" in mode and not path.exists():
            path.write_text(json.dumps(self._default_usage_payload(), indent=2) + "\n", encoding="utf-8")
        handle = open(path, mode, encoding="utf-8")
        try:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                logger.debug("Skipping metadata lock (fcntl unavailable)")
            yield handle
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

    def _load_usage(self) -> dict:
        path = self.metadata_usage_path()
        if not path.exists():
            data = self._default_usage_payload()
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return data
        with self._locked_usage_file("r") as handle:
            try:
                return json.load(handle)
            except json.JSONDecodeError:
                logger.warning("Corrupt usage metadata; recalculating from disk")
                return self.recalculate_usage()

    def _write_usage(self, payload: dict):
        with self._locked_usage_file("w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")

    def get_usage(self, *, force_scan: bool = False) -> int:
        if force_scan:
            return self.recalculate_usage()
        payload = self._load_usage()
        return int(payload.get("used_bytes", 0))

    def record_usage_delta(self, delta_bytes: int):
        payload = self._load_usage()
        new_total = max(0, int(payload.get("used_bytes", 0)) + delta_bytes)
        payload["used_bytes"] = new_total
        payload["last_delta_update"] = datetime.now(timezone.utc).isoformat()
        self._write_usage(payload)

    def recalculate_usage(self) -> int:
        total = 0
        for root, dirs, files in os.walk(self.root):
            root_path = Path(root)
            if self.metadata_root in (root_path, *root_path.parents):
                continue
            # skip metadata directory explicitly
            dirs[:] = [d for d in dirs if (root_path / d) != self.metadata_root]
            for filename in files:
                file_path = root_path / filename
                if self._contains_metadata(file_path):
                    continue
                try:
                    total += file_path.stat().st_size
                except FileNotFoundError:
                    continue
        payload = self._default_usage_payload()
        payload["used_bytes"] = total
        payload["last_full_scan"] = datetime.now(timezone.utc).isoformat()
        payload["last_delta_update"] = payload["last_full_scan"]
        self._write_usage(payload)
        return total

    def enforce_quota(self, delta_bytes: int):
        quota = self.key_config.quota
        hard = quota.hard_limit_bytes
        soft = quota.soft_limit_bytes
        current = self.get_usage()
        projected = max(0, current + delta_bytes)
        if hard is not None and projected > hard:
            raise QuotaExceededError(
                f"Operation would exceed hard limit ({projected} > {hard} bytes)"
            )
        if soft is not None and projected > soft:
            logger.warning(
                "Soft quota exceeded (projected %s bytes > %s)", projected, soft
            )
