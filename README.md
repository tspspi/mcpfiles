# MCP Files MCP Server

MCP Files exposes a constrained filesystem workspace (notes, knowledge bases, code artifacts) to LLM agents and automations. It supports `stdio` and
`remote` HTTP mode.

## Features

- Tool suite for listing, reading, writing, patching, deleting, and stat’ing files and project directory helpers.
- Per API key roots, extension whitelists, recursive-delete flags, quotas, and logging overrides.
- Structured logging/tracing with per security domain toggles and optional dedicated log files.

## Installation

Either from PyPi

```
pip install mcpfiles
```

or via

```
git clone TODO
pip install -e .
```

## Quick Start

1. **Create a configuration file** at `~/.config/mcpfiles.conf` (or another path). Each entry in `api_keys` defines a root directory and permissions.
2. **Run in stdio mode** (default):
   ```sh
   mcpfiles [--config ~/.config/mcpfiles.conf]
   ```
   This binds the server to the root defined in the `stdio` block.
3. **Run in remote HTTP mode**:
   ```sh
   mcpfiles --remote
   ```
   The `--remote` flag is shorthand for `--transport remotehttp` and requires `remote_server` and `api_keys` blocks in the configuration file. Authentication accepts `Authorization: Bearer`, `X-API-Key`, or `?api_key=` query string tokens.
4. **Generate or rotate an API key** for an existing entry:
   ```sh
   mcpfiles --genkey knowledge-base
   ```
   The command prints the plaintext secret once once on `stdout` and patches the matching `api_keys[].kdf` configuration.

### Logging & Tracing

- Global defaults live under the top-level `logging` block. Per-key overrides inherit those defaults and may specify their own log file, categories (`trace_access`, `trace_write`, `trace_delete`, `trace_metadata`) and `debug_all`.
- Each MCP tool invocation emits one structured log entry containing the key ID, optional project ID, category, path, status and compact metrics (bytes written, entries listed, etc.).

## Configuration

The default path for the configuration file is `~/.config/mcpfiles.conf`. This path can be overriden with `--config /path/to/config.json`. To generate API keys one can utilize

```
mcpfiles --genkey <id>
```

An example is shown below:

```json
{
  "mode": "remotehttp",
  "stdio": {
    "root": "/srv/mcpfiles/stdio",
    "projects_enabled": false,
    "extension_whitelist": [".md", ".txt", ".json"],
    "mime_validation": true,
    "allow_nonempty_delete": false,
    "immutable_paths": ["reference"],
    "quota": { "soft_limit_bytes": 268435456, "hard_limit_bytes": 322122547 },
    "logging": { "level": "INFO", "trace_access": true, "trace_write": true }
  },
  "logging": {
    "level": "INFO",
    "logfile": "/var/log/mcpfiles/main.log",
    "trace_access": true,
    "trace_write": true,
    "trace_delete": false,
    "trace_metadata": false,
    "debug_all": false
  },
  "remote_server": {
    "transport": { "uds": "/var/run/mcpfiles.sock" }
  },
  "api_keys": [
    {
      "id": "knowledge-base",
      "kdf": { "...": "..." },
      "root": "/srv/mcpfiles/agentA",
      "projects_enabled": true,
      "extension_whitelist": [".md", ".txt"],
      "mime_validation": true,
      "allow_nonempty_delete": false,
      "immutable_paths": ["reference"],
      "quota": { "soft_limit_bytes": 536870912, "hard_limit_bytes": 644245094 },
      "logging": { "logfile": "/var/log/mcpfiles/agentA.log" }
    }
  ]
}
```

## Workspace Model

- Each stdio run is limited to the root resolved from the `stdio` block 
- Remote mode: Every API key maps to a root directory. When `project_id` is provided, the server resolves into `<root>/.projects/<uuid>/`. Access to `.projects` outside the requested `UUID` is denied.
- An optional extension whitelist per root ensures agents only create/modify allowed file types (suffix checks plus optional `file --mime-type` verification when `mime_validation` is enabled).
- Optional quotas (`soft_limit_bytes`, `hard_limit_bytes`) cap total storage consumption per key/project. Hard limits block writes. Soft limits only log events (no response flag), letting agents query usage on demand.
- Hidden `.metadata/usage.json` directories store accounting data and are automatically excluded from MCP listings/reads. Manual tampering is prevented by treating `.metadata` as immutable.
- Immutable paths (relative to the key/project root) can expose reference material while prohibiting writes; mutation requests targeting these paths return an authorization error.
- Recursive deletions require `allow_nonempty_delete=true` in the key config; otherwise only empty directories can be removed.

## FreeBSD Daemon Setup

- Copy the sample `rc.d` script (`contrib/freebsd/mcpfiles`) into `/usr/local/etc/rc.d/mcpfiles` and make it executable:

```sh
install -m 755 contrib/freebsd/mcpfiles /usr/local/etc/rc.d/mcpfiles
```

- Create a dedicated runtime account with no login shell (adjust UID/GID as needed):

```sh
pw useradd mcpfiles -d /nonexistent -s /usr/sbin/nologin
```

- Configure the service in `/etc/rc.conf`:

```sh
mcpfiles_enable="YES"
mcpfiles_user="mcpfiles"
mcpfiles_group="mcpfiles"
mcpfiles_config="/usr/local/etc/mcpfiles.conf"
mcpfiles_transport="remotehttp"
mcpfiles_logfile="/var/log/mcpfiles/remote.log"
mcpfiles_env="FASTMCP_LOG_LEVEL=INFO"
```

The script automatically feeds those variables into `daemon(8)` so the service runs in the background with PID/log files under `/var/run` and `/var/log`. Override `mcpfiles_env` to pass extra environment variables (e.g., `PYTHONPATH=/usr/local/lib/mcpfiles`).

- Manage the service via the standard rc interface: `service mcpfiles start`, `service mcpfiles stop`, `service mcpfiles restart`, `service mcpfiles status`. The `required_files` guard prevents startup when the config file is missing, mirroring the CLI’s existing validation.
- Apply new API keys or quota rules without downtime via `service mcpfiles reload`, which sends `SIGHUP` to the daemon so it re-reads the configuration while keeping the existing TCP/UDS listener online.
- To run the same configuration manually without installing the rc script, launch it with `daemon(8)` directly:

```sh
daemon -f -p /var/run/mcpfiles.pid -u mcpfiles -o /var/log/mcpfiles/remote.log \
  /usr/local/bin/mcpfiles --config /usr/local/etc/mcpfiles.conf --transport remotehttp
```

Use `service mcpfiles reload` after editing the configuration or rotating API keys so the running daemon re-reads the JSON without interrupting clients; reserve `restart` for listener changes (host/port/UDS) or code upgrades.

## Connecting with Agents

### Codex example (stdio mode)

```toml
[mcp_servers.mcpfiles]
command = "mcpfiles"
args = [
  "--config", "/home/exampleuser/.config/mcpfiles.conf"
]
startup_timeout_sec = 300

[mcp_servers.mcpfiles.env]
# Optional overrides, e.g. logging level or PYTHONPATH if running from source
FASTMCP_LOG_LEVEL = "INFO"
```

### Codex example (remote mode)

```toml
[mcp_servers.mcpfiles]
url = "http://127.0.0.1:7889/mcp/mcp?api_key=XXXXXX"
```

### JSON based MCP configuration

```jsonc
{
  "name": "mcpfiles",
  "type": "mcp",
  "transport": {
    "type": "http",
    "url": "http://127.0.0.1:8080/mcp",
    "headers": {
      "Authorization": "Bearer <PLAINTEXT_API_KEY>"
    }
  },
  "tools": {
    "allowed": [
      "list_dir",
      "read_file",
      "write_file",
      "apply_patch",
      "delete_file",
      "create_directory",
      "remove_directory",
      "get_metadata",
      "stat_tree"
    ]
  }
}
```

## Salting the System Prompt

When giving the LLM these tools, include a short _workspace contract_ in the system message. A possible example snippet:

```
You are operating inside a sandbox rooted at /srv/mcpfiles/agentA. All paths must remain inside this tree.
- Immutable paths: reference/, templates/ — treat them as read-only.
- Allowed extensions: .md, .txt, .json. Others will be rejected.
- Use list_dir/stat_tree/get_metadata to plan before writing. Large writes count against quota.
- apply_patch accepts Codex-style (*** Begin Patch …) or plain unified diff patches; include correct hunks so validation passes.
- Projects: pass project_id to work inside .projects/<uuid>.
- Typical workflow: list_dir -> read_file -> plan -> write_file/apply_patch -> get_metadata/stat_tree to verify.
```


