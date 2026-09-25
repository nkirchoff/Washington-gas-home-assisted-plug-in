"""Tests for signing in through My Washington Gas and its hand-off into Opower."""

from __future__ import annotations

import pytest

from custom_components.washington_gas.api import (
    LOGIN_OPOWER,
    LOGIN_WASHINGTONGAS,
    CannotConnect,
    CaptchaRequired,
    InvalidAuth,
    LoginStepError,
    MfaRequired,
    Portal,
    WashingtonGasClient,
)
from custom_components.washington_gas.errors import (
    STEP_HANDOFF,
    STEP_OPOWER_API,
    STEP_OPOWER_DIRECT,
    STEP_PORTAL_LOGIN,
)

from .fake_opower import (
    ACCOUNT_NUMBER,
    EMBEDDED_TOKEN,
    PASSWORD,
    SAML_ASSERTION,
    SSO_CODE,
    USERNAME,
    FakeOpower,
)


def _site(mode: str | None, **kwargs) -> FakeOpower:
    # Like the real world: the My Washington Gas login doesn't work on wgl.opower.com.
    kwargs.setdefault("direct_login_works", False)
    return FakeOpower.with_history(portal_mode=mode, **kwargs)


def _client(site: FakeOpower, password: str = PASSWORD, **kwargs) -> WashingtonGasClient:
    return WashingtonGasClient(site, USERNAME, password, **kwargs)  # type: ignore[arg-type]


def _steps(client: WashingtonGasClient) -> list[tuple[str, bool]]:
    return [(attempt.step, attempt.ok) for attempt in client.login_report]


def _assert_nothing_secret_leaked(client: WashingtonGasClient) -> None:
    text = " ".join(client.trace) + " ".join(str(attempt) for attempt in client.login_report)
    for secret in (PASSWORD, SAML_ASSERTION, SSO_CODE, EMBEDDED_TOKEN, "target=hea"):
        assert secret not in text


@pytest.mark.parametrize("mode", ["saml", "postback", "redirect", "token"])
async def test_hand_off_styles(mode: str) -> None:
    """Each kind of hand-off gets from My Washington Gas into Opower's API."""
    site = _site(mode)
    client = _client(site)
    await client.async_login()

    assert client.login_method == LOGIN_WASHINGTONGAS
    assert client.portal == Portal("wgl", "wgl")
    accounts = await client.async_get_accounts()
    assert accounts[0].account_id == ACCOUNT_NUMBER
    assert await client.async_get_forecasts(accounts)
    assert _steps(client) == [(STEP_PORTAL_LOGIN, True), (STEP_HANDOFF, True), (STEP_OPOWER_API, True)]
    # The password only went to washingtongas.com, and the direct login wasn't needed.
    assert site.password_sent_to == ["my.washingtongas.com"]
    assert not any("account/signin" in url for _, url, _ in site.calls)
    # Sign Out was never clicked.
    assert site.portal_session
    _assert_nothing_secret_leaked(client)


async def test_embedded_token_ignores_other_widgets_tokens() -> None:
    """A map widget's accessToken on the same page isn't mistaken for Opower's."""
    site = _site("token")
    client = _client(site)
    await client.async_login()
    assert client._access_token == EMBEDDED_TOKEN


async def test_remembered_method_skips_the_other_path() -> None:
    """Once My Washington Gas has worked, the direct Opower login isn't tried."""
    site = _site("no_handoff", direct_login_works=True)
    client = _client(site, portal=Portal("wgl", "wgl"), login_method=LOGIN_WASHINGTONGAS)
    with pytest.raises(LoginStepError):
        await client.async_login()
    assert not any("account/signin" in url for _, url, _ in site.calls)


async def test_wrong_password() -> None:
    """A wrong password is reported as such when My Washington Gas says so."""
    site = _site("saml")
    client = _client(site, password="wrong")
    with pytest.raises(InvalidAuth):
        await client.async_login()
    assert (STEP_PORTAL_LOGIN, False) in _steps(client)
    assert (STEP_OPOWER_DIRECT, False) in _steps(client)
    assert not site.portal_session


async def test_unexplained_bounce_is_not_called_a_wrong_password() -> None:
    """If the sign-in page comes back without saying why, don't blame the password.

    The page always carries a hidden "Invalid username or password" label,
    which mustn't count as the site rejecting the login.
    """
    client = _client(_site("ignores_form"))
    with pytest.raises(LoginStepError) as err:
        await client.async_login()
    assert not isinstance(err.value, InvalidAuth)
    assert err.value.step == STEP_PORTAL_LOGIN
    assert err.value.host == "my.washingtongas.com"


async def test_mfa_stops() -> None:
    """A verification code prompt stops the login instead of guessing."""
    client = _client(_site("mfa"))
    with pytest.raises(MfaRequired) as err:
        await client.async_login()
    assert err.value.step == STEP_PORTAL_LOGIN


async def test_captcha_stops_before_sending_the_password() -> None:
    """A CAPTCHA on the sign-in page stops the login before anything is posted."""
    site = _site("captcha")
    client = _client(site)
    with pytest.raises(CaptchaRequired):
        await client.async_login()
    assert "my.washingtongas.com" not in site.password_sent_to


async def test_javascript_only_sign_in_page() -> None:
    """A sign-in page with no HTML form is reported, with its scripts in the trace."""
    client = _client(_site("js_only"))
    with pytest.raises(LoginStepError) as err:
        await client.async_login()
    assert err.value.step == STEP_PORTAL_LOGIN
    assert "JavaScript" in str(err.value)
    assert any("my.washingtongas.com/portal/js/login.bundle.js" in line for line in client.trace)


async def test_no_usage_link() -> None:
    """Signed in, but nothing leads to Opower: the hand-off step fails."""
    client = _client(_site("no_handoff"))
    with pytest.raises(LoginStepError) as err:
        await client.async_login()
    assert err.value.step == STEP_HANDOFF
    assert _steps(client)[:2] == [(STEP_PORTAL_LOGIN, True), (STEP_HANDOFF, False)]


async def test_link_without_sso_ends_on_opower_sign_in() -> None:
    """A usage link that drops the session lands on Opower's sign-in page."""
    client = _client(_site("opower_wall"))
    with pytest.raises(LoginStepError) as err:
        await client.async_login()
    assert err.value.step == STEP_HANDOFF
    assert err.value.host == "wgl.opower.com"
    assert "sign-in page" in str(err.value)


async def test_opower_rejects_the_hand_off() -> None:
    """Reaching Opower without a working session fails at the API step, with its status."""
    client = _client(_site("bad_assertion"))
    with pytest.raises(LoginStepError) as err:
        await client.async_login()
    assert err.value.step == STEP_OPOWER_API
    assert err.value.status == 401
    assert err.value.host == "wgl.opower.com"


async def test_falls_back_to_direct_opower_login() -> None:
    """People with a separate Opower account still get in when the hand-off fails."""
    site = _site("no_handoff", direct_login_works=True)
    client = _client(site)
    await client.async_login()
    assert client.login_method == LOGIN_OPOWER
    assert _steps(client)[-1] == (STEP_OPOWER_DIRECT, True)


async def test_portal_unreachable_uses_direct_answer() -> None:
    """If my.washingtongas.com is down, the direct login's answer is reported."""
    client = _client(_site(None), password="wrong")
    with pytest.raises(InvalidAuth):
        await client.async_login()


async def test_hand_off_error_wins_over_direct_rejection() -> None:
    """A failed hand-off isn't reported as a wrong password just because the direct login said no."""
    client = _client(_site("no_handoff"))
    with pytest.raises(CannotConnect) as err:
        await client.async_login()
    assert not isinstance(err.value, InvalidAuth)
    assert isinstance(err.value, LoginStepError)
    assert err.value.step == STEP_HANDOFF


async def test_each_login_starts_fresh() -> None:
    """Logging in again (every update) works and doesn't reuse a stale session."""
    site = _site("saml")
    client = _client(site)
    await client.async_login()
    await client.async_login()
    assert await client.async_get_accounts()
    assert _steps(client) == [(STEP_PORTAL_LOGIN, True), (STEP_HANDOFF, True), (STEP_OPOWER_API, True)]
