"""Filesystem operations built on top of WorkspaceManager."""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional, cast
from uuid import UUID

from mcpfiles.workspace import (
    ExtensionNotAllowedError,
    ImmutablePathError,
    MetadataAccessError,
    PathOutsideWorkspace,
    QuotaExceededError,
    WorkspaceManager,
)

logger = logging.getLogger(__name__)


def _serialize_stat(path: Path) -> Dict[str, object]:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "created": datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(),
        "mode": oct(stat.st_mode & 0o777),
    }


@dataclass
class PatchOperation:
    op_type: Literal["update", "add", "delete"]
    path: str
    diff_text: Optional[str] = None
    content: Optional[str] = None


_HUNK_RE = re.compile(
    r"@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def _join_patch_lines(lines: List[str]) -> str:
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _extract_add_content(lines: List[str]) -> str:
    if not lines:
        return ""
    processed = []
    for line in lines:
        processed.append(line[1:] if line.startswith("+") else line)
    return "\n".join(processed) + "\n"


def _parse_codex_patch(patch_text: str) -> List[PatchOperation]:
    operations: List[PatchOperation] = []
    current_block: List[str] = []
    inside_block = False
    for raw_line in patch_text.splitlines():
        stripped = raw_line.strip()
        if stripped == "*** Begin Patch":
            if inside_block:
                raise ValueError("Nested patch block detected")
            inside_block = True
            current_block = []
            continue
        if stripped == "*** End Patch":
            if not inside_block:
                raise ValueError("End patch marker without matching begin marker")
            operations.extend(_parse_patch_block(current_block))
            current_block = []
            inside_block = False
            continue
        if inside_block:
            current_block.append(raw_line)
        elif stripped:
            raise ValueError("Unexpected content outside patch block")
    if inside_block:
        raise ValueError("Patch block missing *** End Patch marker")
    if not operations:
        raise ValueError("No file operations were found in the patch payload")
    return operations


def _parse_patch_block(lines: List[str]) -> List[PatchOperation]:
    operations: List[PatchOperation] = []
    header: Optional[str] = None
    buffer: List[str] = []
    for line in lines:
        if line.startswith("*** "):
            if line.strip() == "*** End of File":
                continue
            if header is not None:
                operations.append(_build_operation_from_section(header, buffer))
                buffer = []
            header = line.strip()
            continue
        if header is None:
            if line.strip():
                raise ValueError("Patch content must follow a file directive")
            continue
        buffer.append(line)
    if header is not None:
        operations.append(_build_operation_from_section(header, buffer))
    return operations


def _build_operation_from_section(header: str, lines: List[str]) -> PatchOperation:
    prefix, _, raw_path = header.partition(":")
    path = raw_path.strip()
    if not path:
        raise ValueError("Patch directive missing a target file path")

    if prefix == "*** Update File":
        diff_text = _join_patch_lines(lines)
        if not diff_text.strip():
            raise ValueError(f"Patch for {path} is empty")
        return PatchOperation(op_type="update", path=path, diff_text=diff_text)
    if prefix == "*** Add File":
        content = _extract_add_content(lines)
        return PatchOperation(op_type="add", path=path, content=content)
    if prefix == "*** Delete File":
        return PatchOperation(op_type="delete", path=path)
    raise ValueError(f"Unsupported patch directive '{header}'")


def _parse_plain_diff(path: Optional[str], patch_text: str) -> List[PatchOperation]:
    if not path:
        raise ValueError("When submitting unified diff patches you must provide a path parameter")
    diff_text = patch_text if patch_text.endswith("\n") else patch_text + "\n"
    if "@@" not in diff_text:
        raise ValueError("Unified diff patches must contain at least one hunk (@@ header)")
    return [PatchOperation(op_type="update", path=path, diff_text=diff_text)]


def _apply_unified_diff_to_text(original_text: str, diff_text: str) -> str:
    orig_lines = original_text.splitlines(keepends=True)
    diff_lines = diff_text.splitlines(keepends=True)
    result: List[str] = []
    orig_index = 0
    i = 0
    hunk_applied = False

    def append_until(target_index: int):
        nonlocal orig_index
        if target_index < 0:
            target_index = 0
        if target_index < orig_index:
            raise ValueError("Patch hunks overlap or are misordered")
        if target_index > len(orig_lines):
            raise ValueError("Patch references lines beyond the end of the file")
        result.extend(orig_lines[orig_index:target_index])
        orig_index = target_index

    while i < len(diff_lines):
        line = diff_lines[i]
        if line.startswith("@@"):
            match = _HUNK_RE.match(line.strip())
            if not match:
                raise ValueError(f"Invalid hunk header: {line.strip()}")
            old_start = int(match.group("old_start"))
            append_until(old_start - 1)
            i += 1
            while i < len(diff_lines):
                current = diff_lines[i]
                if current.startswith("@@"):
                    break
                if current.startswith(" "):
                    if orig_index >= len(orig_lines) or orig_lines[orig_index] != current[1:]:
                        raise ValueError("Context line mismatch while applying patch")
                    result.append(orig_lines[orig_index])
                    orig_index += 1
                elif current.startswith("-"):
                    if orig_index >= len(orig_lines) or orig_lines[orig_index] != current[1:]:
                        raise ValueError("Deletion line mismatch while applying patch")
                    orig_index += 1
                elif current.startswith("+"):
                    result.append(current[1:])
                elif current.startswith("\\"):
                    # "\ No newline at end of file" indicator — ignore.
                    pass
                elif not current.strip():
                    result.append(current)
                else:
                    if orig_index >= len(orig_lines) or orig_lines[orig_index] != current:
                        raise ValueError("Context line mismatch while applying patch")
                    result.append(orig_lines[orig_index])
                    orig_index += 1
                i += 1
            hunk_applied = True
            continue
        if line.startswith(("---", "+++", "diff ", "index ")) or not line.strip():
            i += 1
            continue
        raise ValueError(f"Unsupported unified diff header: {line.strip()}")

    if not hunk_applied:
        raise ValueError("Patch did not include any hunks to apply")

    result.extend(orig_lines[orig_index:])
    return "".join(result)


@dataclass
class FileOperations:
    workspace: WorkspaceManager

    # ----------------------------------------------------------------- listing
    def list_directory(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
        pattern: Optional[str] = None,
        include_metadata: bool = False,
    ) -> List[Dict[str, object]]:
        root = self.workspace.resolve_path(path, allow_metadata=False)
        glob_pattern = pattern or "*"
        entries: Iterable[Path]
        if recursive:
            entries = root.rglob(glob_pattern)
        else:
            entries = root.glob(glob_pattern)

        results: List[Dict[str, object]] = []
        for entry in entries:
            if entry == root:
                continue
            if not entry.exists():
                continue
            if self.workspace._contains_metadata(entry):
                continue
            info: Dict[str, object] = {
                "path": self.workspace.relative_path(entry),
                "type": "dir" if entry.is_dir() else "file",
            }
            if include_metadata:
                info.update(_serialize_stat(entry))
            results.append(info)
        return results

    # ------------------------------------------------------------------- reads
    def read_file(
        self,
        path: str,
        *,
        offset: int = 0,
        length: Optional[int] = None,
        encoding: str = "utf-8",
        as_base64: bool = False,
    ) -> Dict[str, object]:
        file_path = self.workspace.resolve_path(path, allow_metadata=False)
        if not file_path.is_file():
            raise FileNotFoundError(f"{path} does not exist or is not a file")
        with open(file_path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(-1 if length is None else length)
        if as_base64:
            payload = base64.b64encode(data).decode("utf-8")
            return {"content": payload, "encoding": "base64", "size": len(data)}
        text = data.decode(encoding)
        return {"content": text, "encoding": encoding, "size": len(data)}

    # ------------------------------------------------------------------- writes
    def write_file(
        self,
        path: str,
        content: str,
        *,
        append: bool = False,
        encoding: str = "utf-8",
    ) -> Dict[str, object]:
        file_path = self.workspace.resolve_path(path, allow_metadata=False)
        self.workspace.ensure_mutable(file_path)
        self.workspace.enforce_extension(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        old_size = file_path.stat().st_size if file_path.exists() else 0
        data = content.encode(encoding)
        delta = len(data) if append or not file_path.exists() else len(data) - old_size
        if not append and file_path.exists():
            delta = len(data) - old_size
        self.workspace.enforce_quota(delta)

        mode = "ab" if append else "wb"
        with open(file_path, mode) as handle:
            handle.write(data)

        new_size = file_path.stat().st_size
        self.workspace.record_usage_delta(new_size - old_size)
        self.workspace.validate_mime(file_path)
        return {"bytes_written": len(data), "size": new_size}

    def apply_patch(self, path: Optional[str], patch: str, *, encoding: str = "utf-8") -> Dict[str, object]:
        if not patch or not patch.strip():
            raise ValueError("Patch payload cannot be empty")

        normalized = patch.lstrip()
        if normalized.startswith("*** Begin Patch"):
            operations = _parse_codex_patch(patch)
        else:
            operations = _parse_plain_diff(path, patch)

        prepared: List[Dict[str, object]] = []
        for operation in operations:
            file_path = self.workspace.resolve_path(operation.path, allow_metadata=False)
            self.workspace.ensure_mutable(file_path)
            if operation.op_type != "delete":
                if operation.op_type == "update" and not file_path.exists():
                    raise FileNotFoundError(f"{operation.path} does not exist for patching")
                if operation.op_type == "add" and file_path.exists():
                    raise FileExistsError(f"{operation.path} already exists")
                if file_path.exists() and file_path.is_dir():
                    raise IsADirectoryError(f"{operation.path} is a directory")
                self.workspace.enforce_extension(file_path)

            record: Dict[str, object] = {
                "operation": operation.op_type,
                "path": file_path,
            }

            if operation.op_type == "delete":
                if not file_path.exists():
                    raise FileNotFoundError(f"{operation.path} does not exist for deletion")
                if file_path.is_dir():
                    raise IsADirectoryError(f"{operation.path} refers to a directory")
                old_size = file_path.stat().st_size
                record.update({"delta": -old_size, "bytes": 0, "content": None})
            else:
                original_text = ""
                old_size = 0
                if file_path.exists():
                    original_text = file_path.read_text(encoding=encoding)
                    old_size = file_path.stat().st_size
                if operation.op_type == "update":
                    if not operation.diff_text:
                        raise ValueError(f"No diff supplied for {operation.path}")
                    new_text = _apply_unified_diff_to_text(original_text, operation.diff_text)
                else:  # add
                    new_text = operation.content or ""
                encoded = new_text.encode(encoding)
                delta = len(encoded) - old_size
                record.update({"delta": delta, "content": new_text, "bytes": len(encoded)})
            prepared.append(record)

        summary = {
            "total_operations": len(prepared),
            "updated": 0,
            "added": 0,
            "deleted": 0,
            "bytes_written": 0,
            "results": [],
        }

        for entry in prepared:
            op_type = entry["operation"]
            file_path: Path = entry["path"]
            delta = entry["delta"]
            self.workspace.enforce_quota(delta)

            if op_type == "delete":
                file_path.unlink()
                summary["deleted"] += 1
                result = {"path": self.workspace.relative_path(file_path), "operation": "delete"}
            else:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                content = cast(str, entry["content"])
                data = content.encode(encoding)
                with open(file_path, "wb") as handle:
                    handle.write(data)
                self.workspace.validate_mime(file_path)
                summary["bytes_written"] += len(data)
                if op_type == "add":
                    summary["added"] += 1
                else:
                    summary["updated"] += 1
                result = {
                    "path": self.workspace.relative_path(file_path),
                    "operation": op_type,
                    "size": len(data),
                }
            self.workspace.record_usage_delta(delta)
            summary["results"].append(result)

        return summary

    # ------------------------------------------------------------------- delete
    def delete_file(self, path: str) -> Dict[str, object]:
        file_path = self.workspace.resolve_path(path, allow_metadata=False)
        self.workspace.ensure_mutable(file_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"{path} is not a file")
        old_size = file_path.stat().st_size
        file_path.unlink()
        self.workspace.record_usage_delta(-old_size)
        return {"removed": True}

    # --------------------------------------------------------------- directories
    def create_directory(self, path: str) -> Dict[str, object]:
        dir_path = self.workspace.resolve_path(path, allow_metadata=False)
        self.workspace.ensure_mutable(dir_path)
        dir_path.mkdir(parents=True, exist_ok=True)
        return {"created": True}

    def remove_directory(self, path: str, *, recursive: bool = False) -> Dict[str, object]:
        dir_path = self.workspace.resolve_path(path, allow_metadata=False)
        self.workspace.ensure_mutable(dir_path)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"{path} is not a directory")
        allow_recursive = self.workspace.key_config.allow_nonempty_delete
        if recursive and not allow_recursive:
            raise PermissionError("Recursive deletion disabled for this API key")
        if not recursive and any(dir_path.iterdir()):
            raise PermissionError("Directory not empty. Enable recursive deletion to remove it.")
        removed_bytes = self._delete_tree(dir_path)
        self.workspace.record_usage_delta(-removed_bytes)
        return {"removed": True, "bytes_freed": removed_bytes}

    def _delete_tree(self, dir_path: Path) -> int:
        total = 0
        if dir_path.is_dir():
            for child in dir_path.iterdir():
                if child.is_dir():
                    total += self._delete_tree(child)
                else:
                    total += child.stat().st_size
                    child.unlink()
            dir_path.rmdir()
        else:
            size = dir_path.stat().st_size
            dir_path.unlink()
            total += size
        return total

    # ----------------------------------------------------------------- metadata
    def get_metadata(self, path: str) -> Dict[str, object]:
        target = self.workspace.resolve_path(path, allow_metadata=False)
        info = _serialize_stat(target)
        info["path"] = self.workspace.relative_path(target)
        info["extension_allowed"] = self.workspace.extension_allowed(target)
        info["mime_allowed"] = self.workspace.validate_mime(target) if target.is_file() else True
        info["immutable"] = False
        try:
            self.workspace.ensure_mutable(target)
        except ImmutablePathError:
            info["immutable"] = True
        return info

    def stat_tree(self, path: str = ".", *, depth: Optional[int] = None) -> Dict[str, int]:
        root = self.workspace.resolve_path(path, allow_metadata=False)
        file_count = 0
        dir_count = 0
        total_bytes = 0
        base_depth = len(root.parts)
        for current_root, dirs, files in os.walk(root):
            current_path = Path(current_root)
            if self.workspace._contains_metadata(current_path):
                dirs[:] = []
                continue
            rel_depth = len(current_path.parts) - base_depth
            if depth is not None and rel_depth > depth:
                dirs[:] = []
                continue
            dir_count += 1
            for filename in files:
                file_path = current_path / filename
                if self.workspace._contains_metadata(file_path):
                    continue
                try:
                    total_bytes += file_path.stat().st_size
                    file_count += 1
                except FileNotFoundError:
                    continue
        return {"path": self.workspace.relative_path(root), "file_count": file_count, "dir_count": dir_count - 1, "total_bytes": total_bytes}

    # --------------------------------------------------------------- projects
    def list_projects(self) -> List[Dict[str, str]]:
        if not self.workspace.key_config.projects_enabled:
            raise PermissionError("Projects not enabled for this API key")
        projects_dir = self.workspace.base_root / ".projects"
        if not projects_dir.exists():
            return []
        results = []
        for entry in projects_dir.iterdir():
            if entry.is_dir():
                results.append({"id": entry.name, "path": str(entry.resolve())})
        return results

    def create_project(self, project_id: str) -> Dict[str, object]:
        if not self.workspace.key_config.projects_enabled:
            raise PermissionError("Projects not enabled for this API key")
        try:
            UUID(project_id)
        except ValueError as exc:
            raise ValueError("project_id must be a valid UUID") from exc
        project_path = self.workspace.base_root / ".projects" / project_id
        project_path.mkdir(parents=True, exist_ok=True)
        return {"project_id": project_id, "path": str(project_path)}
