import base64
from typing import Dict

from mcpfiles.config.api_keys import (
    derive_argon2id_hash,
    ensure_kdf_defaults,
    match_api_key,
    verify_token_against_key,
)
from mcpfiles.config.schema import APIKeyConfig, QuotaConfig


def make_api_key_config(tmp_path, *, key: str = "secret", overrides: Dict | None = None):
    salt = base64.b64encode(b"0123456789abcdef").decode("ascii")
    kdf = ensure_kdf_defaults({"salt": salt})
    kdf["hash"] = derive_argon2id_hash(key, kdf)
    if overrides:
        kdf.update(overrides)
    return APIKeyConfig(
        id="test",
        kdf=kdf,
        root=str(tmp_path),
        projects_enabled=False,
        extension_whitelist=[".md"],
        allow_nonempty_delete=False,
        immutable_paths=[],
        quota=QuotaConfig(),
        logging=None,
        mime_validation=False,
    )


def test_verify_token_against_key(tmp_path):
    config = make_api_key_config(tmp_path, key="secret-value")
    assert verify_token_against_key("secret-value", config)
    assert not verify_token_against_key("wrong-value", config)


def test_match_api_key(tmp_path):
    config = make_api_key_config(tmp_path, key="abc123")
    other = make_api_key_config(tmp_path, key="second", overrides={"hash": "different"})
    keys = [config, other]
    assert match_api_key("abc123", keys) == config
    assert match_api_key("missing", keys) is None
