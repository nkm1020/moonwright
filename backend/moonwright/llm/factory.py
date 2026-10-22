"""Pick the LLM client: explicit kind > ``MOONWRIGHT_LLM`` > credentials present > fixtures.

``.env`` in the project root is loaded first (existing environment variables win), so a
developer can keep ``NEBIUS_API_KEY`` and the model routing out of the shell.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from dotenv import load_dotenv

from moonwright.llm.base import FixtureClient, LLMClient
from moonwright.llm.tokenfactory import RecordingClient, TokenFactoryClient

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
ENV_FILE: Final = PROJECT_ROOT / ".env"
KINDS: Final = ("fixture", "nebius", "record")


def load_env(path: Path = ENV_FILE) -> bool:
    """Load ``path`` into the environment without overriding set variables; True if it existed."""
    return path.is_file() and load_dotenv(path, override=False)


def resolve_kind(kind: str | None = None) -> str:
    """The client kind that :func:`make_client` would build for ``kind`` (None = from env)."""
    if kind:
        return kind.strip().lower()
    env_kind = os.environ.get("MOONWRIGHT_LLM", "").strip().lower()
    if env_kind:
        return env_kind
    return "nebius" if os.environ.get("NEBIUS_API_KEY") else "fixture"


def make_client(kind: str | None = None) -> LLMClient:
    """Build the client for ``kind`` (``fixture`` | ``nebius`` | ``record``).

    Raises ``ValueError`` for an unknown kind and ``LLMError`` when the live client is asked
    for without ``NEBIUS_API_KEY``.
    """
    load_env()
    resolved = resolve_kind(kind)
    if resolved == "fixture":
        return FixtureClient()
    if resolved == "nebius":
        return TokenFactoryClient()
    if resolved == "record":
        return RecordingClient(TokenFactoryClient())
    raise ValueError(f"unknown LLM client kind {resolved!r}; expected one of {', '.join(KINDS)}")
