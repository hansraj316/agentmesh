"""CLI configuration file support.

Defaults for frequently repeated flags live in a JSON config file
(default ``~/.agentmesh/config.json``, override with ``$AGENTMESH_CONFIG``
or the global ``--config PATH`` flag). Precedence, highest first::

    explicit CLI flag > environment variable > config file > built-in default

Only ``db`` ($AGENTMESH_DB) and ``rules`` ($AGENTMESH_ALERTS) have
environment variables today.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agentmesh.alerts import DEFAULT_RULES_PATH
from agentmesh.store import DEFAULT_DB_PATH

DEFAULT_CONFIG_PATH = Path.home() / ".agentmesh" / "config.json"

DEFAULT_PORT = 7777
DEFAULT_THRESHOLD = 20.0

#: Source labels used by ``effective_value`` / ``render_config``.
SOURCE_FLAG = "flag"
SOURCE_ENV = "env"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"

#: Config keys in display order, with a short type description for errors.
_KEY_TYPES = {
    "db": "a string path",
    "rules": "a string path",
    "port": "an integer",
    "threshold": "a number",
    "window": "an integer",
    "since": "an ISO-8601 string",
}

_KEY_ORDER = ("db", "rules", "port", "threshold", "window", "since")


@dataclass
class Config:
    """Parsed config file values; ``None`` means "not set"."""

    db: Optional[str] = None
    rules: Optional[str] = None
    port: Optional[int] = None
    threshold: Optional[float] = None
    window: Optional[int] = None
    since: Optional[str] = None


def default_config_path() -> Path:
    """Resolve the config file path: $AGENTMESH_CONFIG or ~/.agentmesh/config.json."""
    env = os.environ.get("AGENTMESH_CONFIG")
    if env:
        return Path(env)
    # Computed at call time (not the module constant) so a changed HOME
    # (e.g. in tests) is honored.
    return Path.home() / ".agentmesh" / "config.json"


def _validate_value(key: str, value: Any, path: Path) -> Any:
    """Check one config value's type; return it (floats coerced)."""
    expected = _KEY_TYPES[key]
    if isinstance(value, bool):  # bool is an int subclass; never valid here
        raise ValueError("config key %r in %s must be %s, got a boolean" % (key, path, expected))
    if key in ("db", "rules", "since"):
        if not isinstance(value, str):
            raise ValueError(
                "config key %r in %s must be %s, got %s" % (key, path, expected, _type_name(value))
            )
        return value
    if key in ("port", "window"):
        if not isinstance(value, int):
            raise ValueError(
                "config key %r in %s must be %s, got %s" % (key, path, expected, _type_name(value))
            )
        return value
    # threshold: any non-bool number, normalized to float
    if not isinstance(value, (int, float)):
        raise ValueError(
            "config key %r in %s must be %s, got %s" % (key, path, expected, _type_name(value))
        )
    return float(value)


def _type_name(value: Any) -> str:
    return type(value).__name__


def load_config(path: Optional[Union[str, Path]] = None) -> Config:
    """Load the config file; a missing file yields an empty :class:`Config`.

    Raises ValueError on invalid JSON, on unknown keys (naming them), and
    on values of the wrong type.
    """
    config_path = Path(path) if path is not None else default_config_path()
    if not config_path.exists():
        return Config()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("%s is not valid JSON: %s" % (config_path, exc))
    if not isinstance(data, dict):
        raise ValueError("%s must contain a JSON object, got %s" % (config_path, _type_name(data)))
    unknown = sorted(key for key in data if key not in _KEY_TYPES)
    if unknown:
        raise ValueError(
            "unknown config key(s) in %s: %s (supported: %s)"
            % (config_path, ", ".join(unknown), ", ".join(_KEY_ORDER))
        )
    values = {key: _validate_value(key, value, config_path) for key, value in data.items()}
    return Config(**values)


def effective_value(
    name: str,
    flag_value: Any,
    config: Config,
    env_value: Optional[str] = None,
    default: Any = None,
) -> Any:
    """Resolve one setting: CLI flag > env var > config file > default."""
    return _resolve(name, flag_value, config, env_value=env_value, default=default)[0]


def _resolve(
    name: str,
    flag_value: Any,
    config: Config,
    env_value: Optional[str] = None,
    default: Any = None,
) -> Tuple[Any, str]:
    """Like :func:`effective_value` but also reports the winning source."""
    if name not in _KEY_TYPES:
        raise ValueError("unknown setting %r (supported: %s)" % (name, ", ".join(_KEY_ORDER)))
    if flag_value is not None:
        return flag_value, SOURCE_FLAG
    if env_value:
        return env_value, SOURCE_ENV
    config_value = getattr(config, name)
    if config_value is not None:
        return config_value, SOURCE_CONFIG
    return default, SOURCE_DEFAULT


def resolve_settings(config: Config) -> Dict[str, Tuple[Any, str]]:
    """Resolve every setting for the no-flag case: name -> (value, source)."""
    env_values = {
        "db": os.environ.get("AGENTMESH_DB"),
        "rules": os.environ.get("AGENTMESH_ALERTS"),
    }
    defaults = {
        "db": str(DEFAULT_DB_PATH),
        "rules": str(DEFAULT_RULES_PATH),
        "port": DEFAULT_PORT,
        "threshold": DEFAULT_THRESHOLD,
        "window": None,
        "since": None,
    }
    resolved = {}
    for name in _KEY_ORDER:
        resolved[name] = _resolve(
            name, None, config, env_value=env_values.get(name), default=defaults[name]
        )
    return resolved


def render_config(config: Config, resolved: Dict[str, Tuple[Any, str]]) -> str:
    """Render the effective configuration as a markdown table.

    Sources are computed for the no-flag case; explicit CLI flags always win.
    """
    lines: List[str] = [
        "## AgentMesh configuration",
        "",
        "| key | effective value | source |",
        "| --- | --- | --- |",
    ]
    for name in _KEY_ORDER:
        value, source = resolved[name]
        display = "—" if value is None else str(value)
        lines.append("| %s | %s | %s |" % (name, display, source))
    lines.append("")
    lines.append("Explicit command-line flags always override these values.")
    return "\n".join(lines)
