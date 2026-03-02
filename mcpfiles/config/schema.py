"""Pydantic models describing the mcpfiles configuration."""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class LoggingConfig(BaseModel):
    level: str = "INFO"
    logfile: Optional[str] = None
    trace_access: bool = True
    trace_write: bool = True
    trace_delete: bool = False
    trace_metadata: bool = False
    debug_all: bool = False


class QuotaConfig(BaseModel):
    soft_limit_bytes: Optional[int] = None
    hard_limit_bytes: Optional[int] = None

    @field_validator("hard_limit_bytes")
    @classmethod
    def validate_hard_limit(cls, value, info):
        soft = info.data.get("soft_limit_bytes")
        if value is not None and value <= 0:
            raise ValueError("hard_limit_bytes must be positive")
        if value is not None and soft is not None and value < soft:
            raise ValueError("hard_limit_bytes must be >= soft_limit_bytes")
        return value


class APIKeyConfig(BaseModel):
    id: str
    kdf: dict
    root: str
    projects_enabled: bool = False
    extension_whitelist: List[str] = Field(default_factory=list)
    allow_nonempty_delete: bool = False
    immutable_paths: List[str] = Field(default_factory=list)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    logging: Optional[LoggingConfig] = None
    mime_validation: bool = False


class RemoteTransportConfig(BaseModel):
    uds: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None


class RemoteServerConfig(BaseModel):
    api_key_kdf: Optional[dict] = None
    uds: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    transport: Optional[RemoteTransportConfig] = None

    def resolved_endpoint(self) -> dict:
        transport = self.transport
        endpoint = {
            "uds": self.uds,
            "host": self.host,
            "port": self.port,
        }
        if transport is not None:
            endpoint["uds"] = endpoint["uds"] or transport.uds
            endpoint["host"] = endpoint["host"] or transport.host
            endpoint["port"] = endpoint["port"] or transport.port
        return endpoint


class StdIOConfig(BaseModel):
    root: Optional[str] = None
    projects_enabled: bool = False
    extension_whitelist: List[str] = Field(default_factory=list)
    allow_nonempty_delete: bool = False
    immutable_paths: List[str] = Field(default_factory=list)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)
    logging: Optional[LoggingConfig] = None
    mime_validation: bool = False


class Config(BaseModel):
    mode: Literal["stdio", "remotehttp"] = "stdio"
    stdio_root: str = str(Path.home())
    stdio: StdIOConfig = Field(default_factory=StdIOConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    remote_server: Optional[RemoteServerConfig] = None
    api_keys: List[APIKeyConfig] = Field(default_factory=list)
