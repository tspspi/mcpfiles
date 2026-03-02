import logging

from mcpfiles.app.logging_utils import OperationLogger
from mcpfiles.config.schema import APIKeyConfig, LoggingConfig, QuotaConfig


def make_key_config(tmp_path, logging_override: LoggingConfig | None = None) -> APIKeyConfig:
    return APIKeyConfig(
        id="key",
        kdf={"algorithm": "argon2id", "salt": "c2FsdA==", "hash_len": 32},
        root=str(tmp_path),
        projects_enabled=False,
        extension_whitelist=[".md"],
        allow_nonempty_delete=False,
        immutable_paths=[],
        quota=QuotaConfig(),
        logging=logging_override,
        mime_validation=False,
    )


def test_operation_logger_respects_categories(tmp_path, caplog):
    global_cfg = LoggingConfig(
        trace_access=True,
        trace_write=False,
        trace_delete=False,
        trace_metadata=False,
    )
    op_logger = OperationLogger(global_cfg)
    key_config = make_key_config(tmp_path)

    with caplog.at_level(logging.INFO):
        op_logger.log(
            "access",
            key_config,
            "list_dir",
            path="notes",
            project_id=None,
            status="success",
            details={"entries": 1},
        )
    assert "list_dir" in caplog.text

    caplog.clear()
    key_override = LoggingConfig(trace_access=False, trace_write=False, trace_delete=False, trace_metadata=False)
    key_config.logging = key_override
    with caplog.at_level(logging.INFO):
        op_logger.log(
            "access",
            key_config,
            "list_dir",
            path="notes",
            project_id=None,
            status="success",
            details={"entries": 1},
        )
    assert "list_dir" not in caplog.text


def test_operation_logger_writes_per_key_file(tmp_path):
    log_path = tmp_path / "key.log"
    key_override = LoggingConfig(
        logfile=str(log_path),
        trace_access=True,
        trace_write=True,
        trace_delete=True,
        trace_metadata=True,
    )
    op_logger = OperationLogger(LoggingConfig())
    key_config = make_key_config(tmp_path, logging_override=key_override)

    op_logger.log(
        "write",
        key_config,
        "write_file",
        path="notes/file.md",
        project_id="project",
        status="success",
        details={"bytes_written": 10},
    )

    assert log_path.exists()
    assert "write_file" in log_path.read_text(encoding="utf-8")
