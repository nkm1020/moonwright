"""Shared pytest configuration.

The Token Factory client appends one line per SDK call to ``MOONWRIGHT_LLM_LOG`` (default
``backend/out/llm_log.jsonl``). Tests that drive a mock endpoint must never append to that
real log, so every test gets a log path under its own ``tmp_path``; a test that needs the
default behaviour deletes the variable itself.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _llm_log_to_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MOONWRIGHT_LLM_LOG", str(tmp_path / "llm_log.jsonl"))
