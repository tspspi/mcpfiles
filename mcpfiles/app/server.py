"""CLI entrypoint and FastMCP runners for the mcpfiles MCP server."""

import logging
import os
import signal
import stat
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP

from mcpfiles.app.errors import map_exception
from mcpfiles.app.logging_utils import OperationLogger
from mcpfiles.config.api_keys import match_api_key
from mcpfiles.config.config_manager import (
    generate_and_store_api_key,
    get_config,
    parse_arguments,
    reset_cached_config,
)
from mcpfiles.config.schema import APIKeyConfig, Config, QuotaConfig
from mcpfiles.fs_operations import FileOperations
from mcpfiles.workspace import WorkspaceManager

logger = logging.getLogger(__name__)

REQUEST_STATE_KEY = "mcpfiles_key_config"


@dataclass
class MCPFilesServerState:
    config: Config
    stdio_key: APIKeyConfig
    operation_logger: OperationLogger
    reload_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def run_mcp_server():
    """Main CLI entrypoint matching setuptools console script."""
    args = parse_arguments()
    if args.remote and args.transport != "remotehttp":
        logger.debug("Overriding transport to remotehttp because --remote was supplied")
        args.transport = "remotehttp"

    if args.genkey:
        new_key = generate_and_store_api_key(args.config, args.genkey)
        print(new_key)
        return

    config = get_config(args)
    logger.info("Loaded configuration for mode %s", config.mode)

    server, state = _build_fastmcp_server(config)

    if args.transport == "remotehttp":
        _run_remote_http_transport(args, config, server, state)
        return

    _run_stdio_transport(server, state)


def _build_stdio_key_config(config: Config) -> APIKeyConfig:
    stdio_config = config.stdio
    root_value = stdio_config.root or config.stdio_root
    root = Path(root_value).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    quota = stdio_config.quota or QuotaConfig()
    logging_config = stdio_config.logging
    return APIKeyConfig(
        id="stdio-root",
        kdf={},
        root=str(root),
        projects_enabled=stdio_config.projects_enabled,
        extension_whitelist=stdio_config.extension_whitelist,
        allow_nonempty_delete=stdio_config.allow_nonempty_delete,
        immutable_paths=stdio_config.immutable_paths,
        quota=quota,
        logging=logging_config,
        mime_validation=stdio_config.mime_validation,
    )


def _apply_new_config_to_state(state: MCPFilesServerState, new_config: Config) -> None:
    """Swap in a freshly loaded configuration for an existing server state."""
    state.config = new_config
    state.stdio_key = _build_stdio_key_config(new_config)
    state.operation_logger = OperationLogger(new_config.logging)


def _build_fastmcp_server(config: Config) -> tuple[FastMCP, MCPFilesServerState]:
    op_logger = OperationLogger(config.logging)
    state = MCPFilesServerState(
        config=config,
        stdio_key=_build_stdio_key_config(config),
        operation_logger=op_logger,
    )

    @asynccontextmanager
    async def lifespan(app: FastMCP):
        yield state

    server = FastMCP("mcpfiles", lifespan=lifespan)
    _register_file_tools(server, state)
    return server, state


def _register_file_tools(server: FastMCP, state: MCPFilesServerState) -> None:
    """Register filesystem MCP tools on the provided FastMCP server."""

    def _operations(ctx: Context, project_id: Optional[str]) -> FileOperations:
        if ctx is None:
            raise RuntimeError("Context is required when invoking MCP tools")
        key_config = _resolve_key_config(ctx, state)
        workspace = WorkspaceManager(key_config, project_id=project_id)
        return FileOperations(workspace)

    def _log_result(category, operation, key_config, path, project_id, details=None, status="success", error=None):
        state.operation_logger.log(
            category,
            key_config,
            operation,
            path=path,
            project_id=project_id,
            status=status,
            details=details,
            error=error,
        )

    def _execute(category, operation_name, path, project_id, func, key_config):
        try:
            result = func()
        except Exception as exc:
            error_info = map_exception(exc)
            _log_result(
                category,
                operation_name,
                key_config,
                path,
                project_id,
                status="error",
                error=str(exc),
                details={"error_code": error_info.code},
            )
            raise RuntimeError(f"{error_info.code}: {error_info.message}") from exc
        return result

    @server.tool(
        name="list_dir",
        description="List files/directories under the workspace root. Accepts glob pattern, recursion, and metadata toggles; hides `.metadata` and paths outside the sandbox.",
    )
    def list_dir(
        path: str = ".",
        recursive: bool = False,
        pattern: Optional[str] = None,
        include_metadata: bool = False,
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        entries = _execute(
            "access",
            "list_dir",
            path,
            project_id,
            lambda: ops.list_directory(path, recursive=recursive, pattern=pattern, include_metadata=include_metadata),
            ops.workspace.key_config,
        )
        count = len(entries)
        _log_result(
            "access",
            "list_dir",
            ops.workspace.key_config,
            path,
            project_id,
            details={"entries": count},
        )
        return {"entries": entries, "count": count}

    @server.tool(
        name="read_file",
        description="Read a text file (optionally as base64) starting at an offset. Paths must stay inside the sandbox and respect immutable/extension rules.",
    )
    def read_file(
        path: str,
        offset: int = 0,
        length: Optional[int] = None,
        encoding: str = "utf-8",
        as_base64: bool = False,
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "access",
            "read_file",
            path,
            project_id,
            lambda: ops.read_file(path, offset=offset, length=length, encoding=encoding, as_base64=as_base64),
            ops.workspace.key_config,
        )
        _log_result(
            "access",
            "read_file",
            ops.workspace.key_config,
            path,
            project_id,
            details={"size": result.get("size")},
        )
        return result

    @server.tool(
        name="write_file",
        description="Create or update a UTF-8 file under the workspace. Honors extension whitelist, immutable paths, and quota limits; use append=true to append instead of replacing.",
    )
    def write_file(
        path: str,
        content: str,
        append: bool = False,
        encoding: str = "utf-8",
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "write",
            "write_file",
            path,
            project_id,
            lambda: ops.write_file(path, content, append=append, encoding=encoding),
            ops.workspace.key_config,
        )
        _log_result(
            "write",
            "write_file",
            ops.workspace.key_config,
            path,
            project_id,
            details={"bytes_written": result.get("bytes_written"), "size": result.get("size")},
        )
        return result

    @server.tool(
        name="apply_patch",
        description="Apply Codex-style or standard unified diff patches. Validates every hunk before writing and enforces quota/extension rules.",
    )
    def apply_patch(
        patch: str,
        path: Optional[str] = None,
        encoding: str = "utf-8",
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "write",
            "apply_patch",
            path,
            project_id,
            lambda: ops.apply_patch(path, patch, encoding=encoding),
            ops.workspace.key_config,
        )
        summary = {
            "operations": result.get("total_operations"),
            "updated": result.get("updated"),
            "added": result.get("added"),
            "deleted": result.get("deleted"),
        }
        _log_result("write", "apply_patch", ops.workspace.key_config, path, project_id, details=summary)
        return result

    @server.tool(
        name="delete_file",
        description="Delete a single file (not directories) if it exists and is mutable. Updates quota usage automatically.",
    )
    def delete_file(
        path: str,
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "delete",
            "delete_file",
            path,
            project_id,
            lambda: ops.delete_file(path),
            ops.workspace.key_config,
        )
        _log_result("delete", "delete_file", ops.workspace.key_config, path, project_id, details=result)
        return result

    @server.tool(
        name="create_directory",
        description="Create a directory (with parents) inside the workspace. Immutable path rules still apply.",
    )
    def create_directory(
        path: str,
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "write",
            "create_directory",
            path,
            project_id,
            lambda: ops.create_directory(path),
            ops.workspace.key_config,
        )
        _log_result("write", "create_directory", ops.workspace.key_config, path, project_id, details=result)
        return result

    @server.tool(
        name="remove_directory",
        description="Remove a directory. Recursive deletes require the API key’s `allow_nonempty_delete`; otherwise only empty directories can be removed.",
    )
    def remove_directory(
        path: str,
        recursive: bool = False,
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "delete",
            "remove_directory",
            path,
            project_id,
            lambda: ops.remove_directory(path, recursive=recursive),
            ops.workspace.key_config,
        )
        _log_result(
            "delete",
            "remove_directory",
            ops.workspace.key_config,
            path,
            project_id,
            details={"bytes_freed": result.get("bytes_freed"), "recursive": recursive},
        )
        return result

    @server.tool(
        name="get_metadata",
        description="Return size/timestamp/permission info plus immutable/extension status for a path within the workspace.",
    )
    def get_metadata(
        path: str,
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "metadata",
            "get_metadata",
            path,
            project_id,
            lambda: ops.get_metadata(path),
            ops.workspace.key_config,
        )
        _log_result(
            "metadata",
            "get_metadata",
            ops.workspace.key_config,
            path,
            project_id,
            details={"size": result.get("size"), "immutable": result.get("immutable")},
        )
        return result

    @server.tool(
        name="stat_tree",
        description="Walk a subtree (optionally limited by depth) and report aggregate file/dir counts and total bytes, excluding `.metadata`.",
    )
    def stat_tree(
        path: str = ".",
        depth: Optional[int] = None,
        project_id: Optional[str] = None,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id)
        result = _execute(
            "metadata",
            "stat_tree",
            path,
            project_id,
            lambda: ops.stat_tree(path=path, depth=depth),
            ops.workspace.key_config,
        )
        summary = {
            "total_bytes": result.get("total_bytes"),
            "file_count": result.get("file_count"),
            "dir_count": result.get("dir_count"),
        }
        _log_result("metadata", "stat_tree", ops.workspace.key_config, path, project_id, details=summary)
        return result

    @server.tool(
        name="list_projects",
        description="List available project IDs (UUID directories) when projects are enabled for the API key.",
    )
    def list_projects(
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id=None)
        projects = _execute(
            "access",
            "list_projects",
            path=".",
            project_id=None,
            func=lambda: ops.list_projects(),
            key_config=ops.workspace.key_config,
        )
        count = len(projects)
        _log_result("access", "list_projects", ops.workspace.key_config, ".", None, details={"count": count})
        return {"projects": projects, "count": count}

    @server.tool(
        name="create_project",
        description="Create a new `.projects/<uuid>` directory when the API key allows project management. Requires a valid UUID.",
    )
    def create_project(
        project_id: str,
        ctx: Context = None,
    ):
        ops = _operations(ctx, project_id=None)
        result = _execute(
            "write",
            "create_project",
            path=project_id,
            project_id=None,
            func=lambda: ops.create_project(project_id),
            key_config=ops.workspace.key_config,
        )
        _log_result("write", "create_project", ops.workspace.key_config, project_id, None, details=result)
        return result


def _resolve_key_config(ctx: Context, state: MCPFilesServerState) -> APIKeyConfig:
    request = getattr(ctx.request_context, "request", None)
    if request is not None:
        key_from_request = getattr(getattr(request, "state", object()), REQUEST_STATE_KEY, None)
        if key_from_request is not None:
            return key_from_request
    return state.stdio_key


def _run_stdio_transport(server: FastMCP, state: MCPFilesServerState):
    logger.info("Starting stdio MCP server rooted at %s", state.stdio_key.root)
    server.run("stdio")


def _run_remote_http_transport(args, config: Config, server: FastMCP, state: MCPFilesServerState):
    remote_config = config.remote_server
    if remote_config is None:
        raise RuntimeError("remotehttp transport selected but remote_server configuration is missing")
    if not config.api_keys:
        raise RuntimeError("remotehttp transport requires at least one entry under api_keys")

    app = _build_remote_fastapi_app(server, state)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Remote HTTP transport requires uvicorn. Install extras via 'pip install mcpfiles[remote]'") from exc

    uvicorn_kwargs = {
        "app": app,
        "lifespan": "on",
        "log_config": None,
    }

    endpoint = remote_config.resolved_endpoint()

    if endpoint["port"] is not None:
        uvicorn_kwargs["host"] = endpoint["host"] or "0.0.0.0"
        uvicorn_kwargs["port"] = endpoint["port"]
        logger.info(
            "Starting remote MCP server over TCP at %s:%s",
            uvicorn_kwargs["host"],
            uvicorn_kwargs["port"],
        )
    elif endpoint["uds"]:
        uds_path = endpoint["uds"]
        _prepare_uds_socket(uds_path)
        uvicorn_kwargs["uds"] = uds_path
        logger.info("Starting remote MCP server over UDS at %s", uds_path)
    else:
        raise RuntimeError("remote_server configuration must provide either port or uds")

    _install_remote_reload_handler(args, state)
    uvicorn.run(**uvicorn_kwargs)


def _install_remote_reload_handler(args, state: MCPFilesServerState) -> None:
    """Install a SIGHUP handler that reloads configuration for the remote server."""
    if not hasattr(signal, "SIGHUP"):
        logger.debug("SIGHUP not available on this platform; remote reload disabled")
        return

    def _handle_reload(signum, frame):
        del signum, frame
        logger.info("Received SIGHUP; reloading remote configuration from %s", getattr(args, "config", "default path"))
        try:
            with state.reload_lock:
                reset_cached_config()
                new_config = get_config(args)
                if new_config.remote_server is None:
                    logger.error("Reload aborted: remote_server block missing in %s", getattr(args, "config", "config file"))
                    return
                if not new_config.api_keys:
                    logger.error("Reload aborted: api_keys section empty in %s", getattr(args, "config", "config file"))
                    return
                _apply_new_config_to_state(state, new_config)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to reload configuration on SIGHUP")
        else:
            logger.info("Remote configuration reload successful")

    signal.signal(signal.SIGHUP, _handle_reload)

def _build_remote_fastapi_app(server: FastMCP, state: MCPFilesServerState):
    remote_config = state.config.remote_server
    if remote_config is None:
        raise RuntimeError("remote transport selected but remote_server config is missing")

    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse
        from starlette.middleware.base import BaseHTTPMiddleware
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Remote HTTP transport requires FastAPI. Install extras via 'pip install mcpfiles[remote]'") from exc

    API_KEY_QUERY_PARAM = "api_key"
    STATUS_PATH = "/status"
    MCP_MOUNT_PATH = "/mcp"

    mcp_http_app = server.streamable_http_app()

    @asynccontextmanager
    async def fastapi_lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_http_app.router.lifespan_context(mcp_http_app))
            yield

    fastapi_app = FastAPI(title="mcpfiles Remote Server", version="1.0", lifespan=fastapi_lifespan)

    class APIKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.url.path == STATUS_PATH:
                return await call_next(request)
            token = _extract_api_token(request, API_KEY_QUERY_PARAM)
            key_config = match_api_key(token, state.config.api_keys)
            if key_config is None:
                raise HTTPException(status_code=401, detail="Invalid or missing API key")
            request.state.mcpfiles_key_config = key_config
            return await call_next(request)

    fastapi_app.add_middleware(APIKeyMiddleware)

    @fastapi_app.get(STATUS_PATH)
    async def status():
        return JSONResponse({"running": True})

    fastapi_app.mount(MCP_MOUNT_PATH, mcp_http_app)
    return fastapi_app


def _extract_api_token(request, query_param: str) -> Optional[str]:
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = request.headers.get("x-api-key")
    if not token:
        token = request.query_params.get(query_param)
    return token


def _prepare_uds_socket(path: str):
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        current_stat = os.stat(path)
        if stat.S_ISSOCK(current_stat.st_mode):
            os.remove(path)
        else:
            raise RuntimeError(f"UDS path {path} exists and is not a socket")


__all__ = ["run_mcp_server"]
