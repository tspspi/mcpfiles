from pathlib import Path

from mcpfiles.app.logging_utils import OperationLogger
from mcpfiles.app.server import MCPFilesServerState, _apply_new_config_to_state, _build_stdio_key_config
from mcpfiles.config.schema import APIKeyConfig, Config, LoggingConfig, QuotaConfig, StdIOConfig


def test_build_stdio_key_config_uses_stdio_block(tmp_path):
    stdio_root = tmp_path / "custom-stdio"
    quota = QuotaConfig(soft_limit_bytes=100, hard_limit_bytes=200)
    logging_cfg = LoggingConfig(level="DEBUG", trace_access=False)
    config = Config(
        stdio_root=str(tmp_path / "fallback"),
        stdio=StdIOConfig(
            root=str(stdio_root),
            projects_enabled=True,
            extension_whitelist=[".md", ".txt"],
            allow_nonempty_delete=True,
            immutable_paths=["reference"],
            quota=quota,
            logging=logging_cfg,
            mime_validation=True,
        ),
    )

    key = _build_stdio_key_config(config)

    assert Path(key.root) == stdio_root
    assert key.projects_enabled is True
    assert key.extension_whitelist == [".md", ".txt"]
    assert key.allow_nonempty_delete is True
    assert key.immutable_paths == ["reference"]
    assert key.quota.hard_limit_bytes == 200
    assert key.logging.level == "DEBUG"
    assert key.mime_validation is True


def test_build_stdio_key_config_falls_back_to_stdio_root(tmp_path):
    fallback_root = tmp_path / "fallback-only"
    config = Config(stdio_root=str(fallback_root))

    key = _build_stdio_key_config(config)

    assert Path(key.root) == fallback_root
    assert fallback_root.exists()


def test_apply_new_config_to_state_refreshes_state(tmp_path):
    initial_stdio = tmp_path / "stdio-initial"
    new_stdio = tmp_path / "stdio-new"
    initial_config = Config(
        stdio=StdIOConfig(root=str(initial_stdio)),
        logging=LoggingConfig(level="INFO"),
        api_keys=[
            APIKeyConfig(id="alpha", kdf={"algorithm": "argon2id", "hash": "old"}, root=str(tmp_path / "alpha")),
        ],
    )
    new_config = Config(
        stdio=StdIOConfig(root=str(new_stdio)),
        logging=LoggingConfig(level="DEBUG", trace_access=False, trace_write=False, trace_delete=True),
        api_keys=[
            APIKeyConfig(id="beta", kdf={"algorithm": "argon2id", "hash": "new"}, root=str(tmp_path / "beta")),
        ],
    )
    state = MCPFilesServerState(
        config=initial_config,
        stdio_key=_build_stdio_key_config(initial_config),
        operation_logger=OperationLogger(initial_config.logging),
    )

    _apply_new_config_to_state(state, new_config)

    assert state.config is new_config
    assert Path(state.stdio_key.root) == new_stdio
    assert state.operation_logger.global_config.level == "DEBUG"
