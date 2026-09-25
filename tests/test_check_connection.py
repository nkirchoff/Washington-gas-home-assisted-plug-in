"""Tests for scripts/check_connection.py's report."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from .fake_opower import EMBEDDED_TOKEN, PASSWORD, SAML_ASSERTION, SSO_CODE, USERNAME, FakeOpower

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_connection.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_connection", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(script: ModuleType, mode: str | None, password: str = PASSWORD, trace: bool = False) -> tuple[int, str]:
    site = FakeOpower.with_history(portal_mode=mode, direct_login_works=False)
    return await script._report(site, USERNAME, password, trace), ""


def _check_no_secrets(output: str) -> None:
    for secret in (PASSWORD, SAML_ASSERTION, SSO_CODE, EMBEDDED_TOKEN, "target=hea", "1234567890"):
        assert secret not in output


async def test_success(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = await _run(script, "saml", trace=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "OK     My Washington Gas sign-in: Signed in [my.washingtongas.com, HTTP 200]" in out
    assert "OK     Hand-off to Opower: Reached wgl.opower.com [wgl.opower.com, HTTP 200]" in out
    assert "OK     Opower API" in out
    assert "signed in via washingtongas" in out
    assert "POST wgl.opower.com/ei/sso/saml/acs -> 302" in out
    assert "******7890" in out
    _check_no_secrets(out)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("no_handoff", ["FAILED Hand-off to Opower", "[my.washingtongas.com, HTTP 200]", "Requests made"]),
        ("bad_assertion", ["FAILED Opower API", "[wgl.opower.com, HTTP 401]"]),
        ("js_only", ["FAILED My Washington Gas sign-in", "JavaScript", "login.bundle.js"]),
        ("mfa", ["Stopped", "verification code", "doesn't try to get around"]),
        ("captcha", ["Stopped", "CAPTCHA", "doesn't try to get around"]),
        (None, ["FAILED Direct Opower sign-in", "[wgl.opower.com, HTTP 401]", "rejected"]),
    ],
)
async def test_failures_name_the_step(
    script: ModuleType, capsys: pytest.CaptureFixture[str], mode: str | None, expected: list[str]
) -> None:
    code, _ = await _run(script, mode)
    out = capsys.readouterr().out
    assert code == 1
    for text in expected:
        assert text in out
    _check_no_secrets(out)


async def test_wrong_password(script: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = await _run(script, "saml", password="wrong")
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED My Washington Gas sign-in: My Washington Gas rejected the username or password" in out
    assert "my.washingtongas.com" in out
    assert "wrong" not in out.replace("rejected", "")
