"""mcpfiles package initializer."""

from importlib import metadata


def __getattr__(name: str):
    if name == "__version__":
        return metadata.version("mcpfiles")
    raise AttributeError(name)


__all__ = ["__version__"]
