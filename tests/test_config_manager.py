import json
import os
from argparse import Namespace

import pytest

from mcpfiles.config.config_manager import (
    generate_and_store_api_key,
    get_config,
    load_config,
    parse_arguments,
    reset_cached_config,
)
from mcpfiles.config.schema import Config


def sample_config(tmp_path):
    path = tmp_path / "config.json"
    data = {
        "mode": "remotehttp",
        "stdio_root": "/tmp/workspace",
        "logging": {
            "level": "INFO",
            "logfile": None,
            "trace_access": True,
            "trace_write": True,
            "trace_delete": False,
            "trace_metadata": False,
            "debug_all": False,
        },
        "remote_server": {"uds": "/tmp/mcpfiles.sock"},
        "api_keys": [
            {
                "id": "test-key",
                "kdf": {},
                "root": "/tmp/workspaces/test-key",
                "projects_enabled": True,
                "extension_whitelist": [".md"],
                "immutable_paths": ["reference"],
            }
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def test_load_config_valid(tmp_path):
    path, _ = sample_config(tmp_path)
    cfg = load_config(str(path))
    assert cfg.mode == "remotehttp"
    assert cfg.api_keys[0].id == "test-key"


def test_get_config_applies_overrides(tmp_path):
    path, _ = sample_config(tmp_path)
    reset_cached_config()
    args = Namespace(
        config=str(path),
        log_level="DEBUG",
        logfile=str(tmp_path / "test.log"),
        transport="stdio",
        genkey=None,
    )
    cfg = get_config(args)
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.logfile == args.logfile
    reset_cached_config()


def test_parse_arguments_remote_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "--config", "sample.json", "--remote"])
    args = parse_arguments()
    assert args.remote is True
    assert args.transport == "remotehttp"


def test_generate_and_store_api_key_updates_entry(tmp_path):
    path, data = sample_config(tmp_path)
    reset_cached_config()
    new_key = generate_and_store_api_key(str(path), "test-key")
    assert isinstance(new_key, str) and len(new_key) >= 16

    updated = json.loads(path.read_text())
    entry = updated["api_keys"][0]
    kdf = entry["kdf"]
    assert "hash" in kdf and isinstance(kdf["hash"], str)
    assert "salt" in kdf and isinstance(kdf["salt"], str)

    # ensure config remains valid after update
    Config(**updated)


def test_remote_server_transport_block_parses():
    cfg = Config(
        remote_server={
            "transport": {"host": "127.0.0.1", "port": 8080},
        }
    )
    remote = cfg.remote_server
    assert remote is not None
    endpoint = remote.resolved_endpoint()
    assert endpoint["host"] == "127.0.0.1"
    assert endpoint["port"] == 8080
    assert endpoint["uds"] is None


def test_remote_server_transport_prefers_nested_uds():
    cfg = Config(remote_server={"transport": {"uds": "/tmp/mcpfiles.sock"}})
    remote = cfg.remote_server
    endpoint = remote.resolved_endpoint()
    assert endpoint["uds"] == "/tmp/mcpfiles.sock"
