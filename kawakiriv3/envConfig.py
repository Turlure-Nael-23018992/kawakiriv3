"""Configuration read from the process environment, else from `kawakiriv3/.env`
(a minimal `KEY=VALUE` file, never versioned).

Also resolves the `${VAR}` placeholders a job file uses for what must not be
versioned with it (a remote source's host, user, password...).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _envFileValues(envPath):
    values = {}
    if not envPath.exists():
        return values
    for line in envPath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def envValue(key, envPath=None):
    """`key` from the process environment, else from `envPath` (default
    `ENV_PATH`); None when neither defines it."""
    if os.getenv(key):
        return os.getenv(key)
    return _envFileValues(envPath or ENV_PATH).get(key)


def resolvePlaceholders(config, envPath=None):
    """Copy of `config` (a dict, possibly nested) where every `${VAR}` inside
    a string value is replaced by `envValue(VAR)`. Raises ValueError naming
    every variable defined nowhere."""
    missing = []

    def substitute(match):
        value = envValue(match.group(1), envPath)
        if value is None:
            missing.append(match.group(1))
            return match.group(0)
        return value

    def resolve(value):
        if isinstance(value, str):
            return _PLACEHOLDER.sub(substitute, value)
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    resolved = resolve(config)
    if missing:
        raise ValueError(
            f"variable(s) d'environnement non definie(s) : {sorted(set(missing))} "
            f"(a renseigner dans {envPath or ENV_PATH} ou dans l'environnement)"
        )
    return resolved
