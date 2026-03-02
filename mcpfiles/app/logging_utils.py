"""Utilities for structured logging of MCP file operations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from mcpfiles.config.schema import APIKeyConfig, LoggingConfig

CATEGORY_FLAGS = {
    "access": "trace_access",
    "write": "trace_write",
    "delete": "trace_delete",
    "metadata": "trace_metadata",
}


@dataclass
class OperationLogger:
    """Route operation logs to the configured destinations."""

    global_config: LoggingConfig
    _formatter: logging.Formatter = field(
        init=False,
        default=logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"),
    )
    _key_loggers: Dict[str, logging.Logger] = field(init=False, default_factory=dict)

    def _effective_config(self, key_config: APIKeyConfig) -> LoggingConfig:
        if key_config.logging is not None:
            return key_config.logging
        return self.global_config

    def _category_enabled(self, logging_config: LoggingConfig, category: str) -> bool:
        if logging_config.debug_all:
            return True
        flag = CATEGORY_FLAGS.get(category)
        if flag is None:
            return False
        return getattr(logging_config, flag, False)

    def _logger_for_key(self, key_config: APIKeyConfig) -> logging.Logger:
        override = key_config.logging
        if not override or not override.logfile:
            return logging.getLogger("mcpfiles.operations")

        key_id = key_config.id
        if key_id in self._key_loggers:
            return self._key_loggers[key_id]

        logger = logging.getLogger(f"mcpfiles.operations.{key_id}")
        logger.propagate = True
        log_dir = os.path.dirname(os.path.abspath(override.logfile))
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        handler = logging.FileHandler(override.logfile, mode="a", encoding="utf-8")
        handler.setFormatter(self._formatter)
        logger.addHandler(handler)
        self._key_loggers[key_id] = logger
        return logger

    def log(
        self,
        category: str,
        key_config: APIKeyConfig,
        operation: str,
        *,
        path: Optional[str],
        project_id: Optional[str],
        status: str,
        details: Optional[Dict[str, object]] = None,
        error: Optional[str] = None,
    ) -> None:
        logging_config = self._effective_config(key_config)
        if not self._category_enabled(logging_config, category):
            return

        logger = self._logger_for_key(key_config)
        safe_path = path or "-"
        safe_project = project_id or "-"
        message = (
            f"[{status}] category={category} op={operation} key={key_config.id} "
            f"path={safe_path} project={safe_project}"
        )
        if details:
            detail_parts = [f"{name}={value}" for name, value in details.items()]
            if detail_parts:
                message += " " + " ".join(detail_parts)
        if error:
            message += f" error={error}"
        logger.info(message)
