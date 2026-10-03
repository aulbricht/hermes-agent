"""Private immutable inputs shared by guarded Atlas agent initialization.

Snapshots contain the full resolved config and tool schemas in private JSON
strings. They are process-local only: repr, logging, and persistence expose
none of their contents. Callers receive fresh copies on every access.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, repr=False)
class AtlasInitSnapshot:
    _config_json: str = field(repr=False)
    _tools_json: str = field(repr=False)
    _tool_generation: int = field(repr=False)

    def config_copy(self) -> Any:
        """Return a detached config value decoded from the private snapshot."""
        return json.loads(self._config_json)

    def tools_copy(self) -> list[dict[str, Any]]:
        """Return detached tool definitions decoded from the private snapshot."""
        return json.loads(self._tools_json)

    @property
    def tool_generation(self) -> int:
        return self._tool_generation


def capture_atlas_init_snapshot(
    *, config: Any, tool_definitions: list[dict[str, Any]], tool_generation: int
) -> AtlasInitSnapshot:
    """Freeze values already resolved by the caller during ordinary init."""
    if isinstance(tool_generation, bool) or not isinstance(tool_generation, int) or tool_generation < 0:
        raise ValueError("tool_generation must be a non-negative integer")
    config_json = json.dumps(config, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    tools_json = json.dumps(tool_definitions, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return AtlasInitSnapshot(config_json, tools_json, tool_generation)


__all__ = ["AtlasInitSnapshot", "capture_atlas_init_snapshot"]
