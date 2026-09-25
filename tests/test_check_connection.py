"""Tests for scripts/check_connection.py's report."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest

from .fake_opower import ACCOUNT_NUMBER, PASSWORD, SESSION_TOKEN, USERNAME, FakeOpower

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_connection.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_connection", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(script: ModuleType, password: str = PASSWORD, **site_kwargs: Any) -> int:
    site = FakeOpower.with_history(**site_kwargs)
    return await script._report(site, USERNAME, password)


def _check_no_secrets(output: str) -> None:
    for secret in (PASSWORD, SESSION_TOKEN, ACCOUNT_NUMBER):
        assert secret not in output


async def test_success(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code = await _run(script)
    out = capsys.readouterr().out
    assert code == 0
    assert "OK     Sign-in: Signed in [wgl.opower.com, HTTP 204]" in out
    assert "OK     Reading accounts: Found 1 customer record(s) [wgl.opower.com, HTTP 200]" in out
    assert "Using wgl.opower.com" in out
    assert "******7890" in out
    assert "This bill (2026-01-05 to 2026-02-04): 48 therms so far, $91.20" in out
    _check_no_secrets(out)


async def test_wrong_password(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code = await _run(script, password="wrong")
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED Sign-in: Opower rejected the username or password [wgl.opower.com, HTTP 401]" in out
    assert "FAILED Sign-in: No sign-in on this Opower site [wglm.opower.com, HTTP 404]" in out
    assert "A My Washington Gas login won't work there" in out
    assert "https://wgl.opower.com/ei/x/create-account" in out
    assert "wrong" not in out


async def test_outage(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code = await _run(script, login_status=503)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED Sign-in: Sign-in failed [wgl.opower.com, HTTP 503]" in out
    assert "Stopped:" in out
    assert "paste everything above" in out
    _check_no_secrets(out)


async def test_no_accounts(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(FakeOpower, "_customers", return_value={"customers": []}):
        code = await _run(script)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED Reading accounts: Signed in, but no accounts are linked [wgl.opower.com, HTTP 200]" in out
    assert "no gas accounts are linked" in out
