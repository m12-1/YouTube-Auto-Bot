"""
config/settings.py

Single source of truth for all configuration values.
Nothing in this project should read `os.environ` directly except
this file. All values are pulled from environment variables so that
no secret or environment-specific value is ever hardcoded.

The exact secret names below match the platform's existing secret
inventory and must not be renamed.
"""

from __future__ import annotations

import os
from typing import Optional


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Read an environment variable.

    Args:
        name: Environment variable name.
        default: Value returned when the variable is not set.

    Returns:
        The environment variable's value, or `default` if unset.
    """
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# General / runtime settings
# ---------------------------------------------------------------------------
LOG_LEVEL: str = _get_env("LOG_LEVEL", "INFO") or "INFO"
ENVIRONMENT: str = _get_env("ENVIRONMENT", "development") or "development"

# Default retry policy, overridable per-module.
DEFAULT_MAX_ATTEMPTS: int = int(_get_env("DEFAULT_MAX_ATTEMPTS", "3") or 3)
DEFAULT_RETRY_BASE_DELAY_SECONDS: float = float(
    _get_env("DEFAULT_RETRY_BASE_DELAY_SECONDS", "1.0") or 1.0
)

# ---------------------------------------------------------------------------
# Gemini API keys
# ---------------------------------------------------------------------------
GEMINI_KEY_ADVANCED: Optional[str] = _get_env("GEMINI_KEY_ADVANCED")
GEMINI_KEY_FILTER: Optional[str] = _get_env("GEMINI_KEY_FILTER")
GEMINI_KEY_FILTER_2: Optional[str] = _get_env("GEMINI_KEY_FILTER_2")
GEMINI_KEY_IMAGE: Optional[str] = _get_env("GEMINI_KEY_IMAGE")
GEMINI_KEY_LIGHT: Optional[str] = _get_env("GEMINI_KEY_LIGHT")

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------
GH_PAT: Optional[str] = _get_env("GH_PAT")

# ---------------------------------------------------------------------------
# Google service account / sheets
# ---------------------------------------------------------------------------
GOOGLE_SERVICE_ACCOUNT_JSON: Optional[str] = _get_env("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID: Optional[str] = _get_env("SPREADSHEET_ID")

# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------
GROQ_API_KEY: Optional[str] = _get_env("GROQ_API_KEY")

# ---------------------------------------------------------------------------
# Media providers
# ---------------------------------------------------------------------------
PEXELS_API_KEY: Optional[str] = _get_env("PEXELS_API_KEY")
PIXABAY_API_KEY: Optional[str] = _get_env("PIXABAY_API_KEY")

# ---------------------------------------------------------------------------
# Puter
# ---------------------------------------------------------------------------
PUTER_USERNAME: Optional[str] = _get_env("PUTER_USERNAME")
PUTER_PASSWORD: Optional[str] = _get_env("PUTER_PASSWORD")

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: Optional[str] = _get_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: Optional[str] = _get_env("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# YouTube OAuth / Data API
# ---------------------------------------------------------------------------
YOUTUBE_OAUTH_CLIENT_ID: Optional[str] = _get_env("YOUTUBE_OAUTH_CLIENT_ID")
YOUTUBE_OAUTH_CLIENT_SECRET: Optional[str] = _get_env("YOUTUBE_OAUTH_CLIENT_SECRET")
YOUTUBE_OAUTH_REFRESH_TOKEN: Optional[str] = _get_env("YOUTUBE_OAUTH_REFRESH_TOKEN")
YOUTUBE_SEARCH_API_KEY: Optional[str] = _get_env("YOUTUBE_SEARCH_API_KEY")


def get_all_secret_names() -> list[str]:
    """
    Return the full list of secret environment variable names this
    platform depends on. Useful for startup validation / health checks.

    Returns:
        List of environment variable names.
    """
    return [
        "GEMINI_KEY_ADVANCED",
        "GEMINI_KEY_FILTER",
        "GEMINI_KEY_FILTER_2",
        "GEMINI_KEY_IMAGE",
        "GEMINI_KEY_LIGHT",
        "GH_PAT",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GROQ_API_KEY",
        "PEXELS_API_KEY",
        "PIXABAY_API_KEY",
        "PUTER_USERNAME",
        "PUTER_PASSWORD",
        "SPREADSHEET_ID",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "YOUTUBE_OAUTH_CLIENT_ID",
        "YOUTUBE_OAUTH_CLIENT_SECRET",
        "YOUTUBE_OAUTH_REFRESH_TOKEN",
        "YOUTUBE_SEARCH_API_KEY",
    ]


def check_missing_secrets() -> list[str]:
    """
    Check which of the platform's expected secrets are not currently
    set in the environment.

    Returns:
        List of missing environment variable names (empty if all set).
    """
    return [name for name in get_all_secret_names() if not _get_env(name)]
