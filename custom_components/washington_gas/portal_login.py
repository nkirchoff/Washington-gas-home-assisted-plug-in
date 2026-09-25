"""Sign in through My Washington Gas and follow its hand-off into Opower.

A normal Washington Gas customer only has a login for my.washingtongas.com.
The usage pages on wgl.opower.com are reached from there (the Usage / Home
Energy Analysis link), and wgl.opower.com's own sign-in page doesn't accept
that login. So this module does what a browser does:

1. Open my.washingtongas.com and post its sign-in form.
2. Follow the hand-off to Opower. Whatever mechanism the site uses is
   followed as served rather than assumed: HTTP redirects (OIDC and
   token-in-URL hand-offs), auto-submitting SSO forms (SAML POST), ASP.NET
   postback links, or an Opower access token embedded in the usage page
   (the pattern Puget Sound Energy uses).
3. Stop once a page on *.opower.com that isn't its sign-in page is reached.
   Opower's session cookie is then in the cookie jar.

This mirrors how the opower library (github.com/tronikos/opower) signs in
for utilities whose login goes through their own site first, in particular
AES Indiana (ASP.NET portal, SAML to Opower) and PSE (token in the page).

Safety rules:
- The password is only ever posted to an https host under washingtongas.com.
- SSO forms (which carry signed assertions) are only posted to washingtongas.com,
  opower.com or Oracle IDCS hosts.
- A CAPTCHA or a verification code (MFA) stops the login. There is no
  attempt to get around either.
- The trace only records methods, hosts, paths, HTTP statuses and form field
  names. Never query strings, form values, cookies or tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import logging
import re

import aiohttp
from yarl import URL

from .errors import (
    STEP_HANDOFF,
    STEP_PORTAL_LOGIN,
    CaptchaRequired,
    InvalidAuth,
    LoginStepError,
    MfaRequired,
    PortalUnavailable,
)

_LOGGER = logging.getLogger(__name__)

PORTAL_START = URL("https://my.washingtongas.com/")

_MAX_HOPS = 15
# Pages looked at while searching for the hand-off after signing in.
_MAX_HANDOFF_PAGES = 8

_CAPTCHA = re.compile(
    r"g-recaptcha|recaptcha/api\.js|google\.com/recaptcha|hcaptcha\.com|h-captcha|"
    r"challenges\.cloudflare\.com/turnstile|cf-turnstile",
    re.IGNORECASE,
)
_MFA_FIELD = re.compile(
    r"otp|mfa|passcode|one.?time|verif|security.?code|auth.?code|access.?code|2fa|two.?factor",
    re.IGNORECASE,
)
_MFA_TEXT = re.compile(
    r"verification code|one[- ]time (?:pass)?code|security code|enter the code|we sent (?:you )?a code",
    re.IGNORECASE,
)
_BAD_CREDENTIALS = re.compile(
    r"(?:invalid|incorrect|wrong|not valid|doesn'?t match|does not match|not recognized|unable to verify)"
    r"[^.<]{0,60}(?:password|user ?name|user ?id|e-?mail|credentials|log ?in)"
    r"|(?:password|user ?name|user ?id|e-?mail|credentials)[^.<]{0,60}"
    r"(?:invalid|incorrect|wrong|not valid|doesn'?t match|does not match|not recognized)",
    re.IGNORECASE,
)
_SIGN_IN_LINK = re.compile(r"sign ?in|log ?in|logon", re.IGNORECASE)
_SIGN_OUT = re.compile(r"sign ?out|log ?out|logoff", re.IGNORECASE)
_USERNAME_FIELD = re.compile(r"user|e-?mail|login|logon|uid", re.IGNORECASE)
_POSTBACK = re.compile(r"__doPostBack\(\s*['\"]([^'\"]*)['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)")
_OPOWER_URL = re.compile(r"https://[a-z0-9-]+\.opower\.com/[^\s\"'<>\\)]*", re.IGNORECASE)
_OPOWER_HOST = re.compile(r"https://([a-z0-9-]+)\.opower\.com", re.IGNORECASE)
_EMBEDDED_TOKEN = re.compile(
    r"(?:accessToken|access_token|opowerToken|opowerAccessToken)[\"']?\s*[:=]\s*[\"']([A-Za-z0-9\-_.~+/=]{20,})[\"']"
)
_PSE_TOKEN = re.compile(r"var accessToken\s*=\s*[\"']([A-Za-z0-9\-_.~+/=]{20,})[\"']")
# Hidden-only forms that browsers submit by themselves to finish an SSO.
_SSO_FORM_FIELDS = {"SAMLResponse", "SAMLRequest", "wresult", "id_token", "OCIS_REQ_SP"}
_TEXT_INPUT_TYPES = {"text", "email", "tel", ""}


def _where(url: URL) -> str:
    """Host and path only. Query strings can carry signed SSO material."""
    return f"{url.host}{url.path}"


def is_portal_host(url: URL) -> bool:
    """Return True for https URLs on washingtongas.com."""
    host = url.host or ""
    return url.scheme == "https" and (host == "washingtongas.com" or host.endswith(".washingtongas.com"))


def is_opower_host(url: URL) -> bool:
    """Return True for https URLs on opower.com."""
    return url.scheme == "https" and (url.host or "").endswith(".opower.com")


def _is_sso_host(url: URL) -> bool:
    host = url.host or ""
    return (
        is_portal_host(url)
        or is_opower_host(url)
        or (url.scheme == "https" and host.endswith(".identity.oraclecloud.com"))
    )


def _is_opower_sign_in(url: URL) -> bool:
    """Opower's own sign-in pages. Its SSO callbacks can say "login", so that word doesn't count."""
    path = url.path.lower()
    return any(part in path for part in ("sign-in", "/signin", "forgot-password", "register"))


def _is_static(url: URL) -> bool:
    return url.path.lower().endswith(
        (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2")
    )


@dataclass
class _Field:
    name: str
    type: str
    value: str
    checked: bool
    is_button: bool = False


@dataclass
class _Form:
    action: str | None
    fields: list[_Field] = field(default_factory=list)

    @property
    def has_password(self) -> bool:
        return any(f.type == "password" for f in self.fields)

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields if f.name]

    def data(self, clicked: str | None = None) -> dict[str, str]:
        """Return what a browser would submit, pressing the first submit button (or `clicked`)."""
        result: dict[str, str] = {}
        pressed = False
        for f in self.fields:
            if not f.name:
                continue
            if f.type in ("button", "reset", "image", "file"):
                continue
            if f.type in ("checkbox", "radio") and not f.checked:
                continue
            if f.type == "submit":
                if clicked is None and not pressed:
                    pressed = True
                elif f.name != clicked:
                    continue
            result[f.name] = f.value
        return result

    def is_sso_autosubmit(self) -> bool:
        names = set(self.names)
        visible = [f for f in self.fields if f.type not in ("hidden", "submit")]
        return bool(names & _SSO_FORM_FIELDS) and not visible


@dataclass
class _Link:
    url: str
    text: str
    onclick: str = ""


class _PageParser(HTMLParser):
    """Collect forms, links, frames, scripts and visible text from a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.links: list[_Link] = []
        self.frames: list[str] = []
        self.script_srcs: list[str] = []
        self.inline_scripts: list[str] = []
        self.text: list[str] = []
        self._form: _Form | None = None
        self._link: _Link | None = None
        self._select: _Field | None = None
        self._in_script = False
        self._in_style = False
        self._script: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {key.lower(): (value or "") for key, value in attrs}
        if tag == "form":
            self._form = _Form(a.get("action") or None)
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            self._form.fields.append(
                _Field(a.get("name", ""), a.get("type", "text").lower(), a.get("value", ""), "checked" in a)
            )
        elif tag == "button" and self._form is not None and a.get("name"):
            button_type = a.get("type", "submit").lower()
            self._form.fields.append(_Field(a["name"], button_type, a.get("value", ""), False, is_button=True))
        elif tag == "select" and self._form is not None:
            self._select = _Field(a.get("name", ""), "select", "", True)
            self._form.fields.append(self._select)
        elif tag == "option" and self._select is not None:
            if not self._select.value or "selected" in a:
                self._select.value = a.get("value", "")
        elif tag == "a":
            self._link = _Link(a.get("href", ""), "", a.get("onclick", ""))
            self.links.append(self._link)
        elif tag in ("iframe", "frame") and a.get("src"):
            self.frames.append(a["src"])
        elif tag == "script":
            self._in_script = True
            self._script = []
            if a.get("src"):
                self.script_srcs.append(a["src"])
        elif tag == "style":
            self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag == "a":
            self._link = None
        elif tag == "select":
            self._select = None
        elif tag == "script":
            self._in_script = False
            if self._script:
                self.inline_scripts.append("".join(self._script))
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script.append(data)
            return
        if self._in_style:
            return
        if self._link is not None:
            self._link.text += data
        if data.strip():
            self.text.append(data.strip())


@dataclass
class Page:
    """A page as a browser ends up on it, after redirects."""

    url: URL
    status: int
    html: str
    _parsed: _PageParser | None = None

    @property
    def parsed(self) -> _PageParser:
        if self._parsed is None:
            self._parsed = _PageParser()
            self._parsed.feed(self.html)
            self._parsed.close()
        return self._parsed

    @property
    def text(self) -> str:
        return " ".join(self.parsed.text)

    def login_form(self) -> _Form | None:
        return next((form for form in self.parsed.forms if form.has_password), None)

    def resolve(self, target: str | None) -> URL:
        """Resolve a link or form action against this page, like a browser does."""
        if not target:
            return self.url
        return self.url.join(URL(target.replace("\\/", "/").strip(), encoded=True))


@dataclass(frozen=True)
class OpowerHandoff:
    """Where the hand-off ended up."""

    # The opower.com subdomain reached, when the hand-off landed on Opower.
    subdomain: str | None
    # An Opower access token from the usage page, when the site embeds one.
    token: str | None


class PortalLogin:
    """Sign in on my.washingtongas.com and follow the hand-off into Opower."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        user_agent: str,
        timeout: aiohttp.ClientTimeout,
        trace: list[str],
    ) -> None:
        """Initialize. trace collects a redacted record of each request."""
        self._session = session
        self._username = username
        self._password = password
        self._user_agent = user_agent
        self._timeout = timeout
        self._trace = trace
        # Where each step ended, for the login report: (host, HTTP status).
        self.signed_in_at: tuple[str | None, int] | None = None
        self.handoff_at: tuple[str | None, int] | None = None
        self.last_at: tuple[str | None, int | None] = (None, None)

    def _note(self, line: str) -> None:
        self._trace.append(line)
        _LOGGER.debug("%s", line)

    async def _fetch(
        self,
        method: str,
        url: URL,
        step: str,
        data: dict[str, str] | None = None,
        referer: URL | None = None,
    ) -> Page:
        """Request a page, following redirects by hand so signed URLs keep their exact encoding."""
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if referer is not None:
            headers["Referer"] = str(referer)
        for _ in range(_MAX_HOPS):
            if url.scheme != "https":
                raise LoginStepError(step, f"Refusing to follow a non-https link to {url.host}", url.host)
            try:
                async with self._session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                    timeout=self._timeout,
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location")
                    body = "" if status in (301, 302, 303, 307, 308) else await resp.text(errors="replace")
            except (aiohttp.ClientError, TimeoutError) as err:
                error = PortalUnavailable if step == STEP_PORTAL_LOGIN and method == "GET" else LoginStepError
                raise error(step, f"Could not reach {url.host}: {type(err).__name__}", url.host) from err
            self._note(f"{method} {_where(url)} -> {status}")
            self.last_at = (url.host, status)
            if status in (301, 302, 303, 307, 308):
                if not location:
                    raise LoginStepError(step, "Redirect without a Location header", url.host, status)
                try:
                    url = url.join(URL(location, encoded=True))
                except ValueError as err:
                    raise LoginStepError(step, "Invalid redirect", url.host, status) from err
                if status in (307, 308) and data is not None:
                    # 307/308 re-send the body. Never let that carry form data off the SSO hosts.
                    if not _is_sso_host(url):
                        raise LoginStepError(step, f"Refusing to re-post a form to {url.host}", url.host, status)
                else:
                    method, data = "GET", None
                continue
            return Page(url, status, body)
        raise LoginStepError(step, "Too many redirects", url.host)

    async def async_login(self) -> OpowerHandoff:
        """Sign in and follow the hand-off.

        :raises InvalidAuth: My Washington Gas said the username or password is wrong
        :raises CaptchaRequired: the sign-in page has a CAPTCHA
        :raises MfaRequired: My Washington Gas asked for a verification code
        :raises PortalUnavailable: my.washingtongas.com couldn't be reached
        :raises LoginStepError: anything else, naming the step that failed
        """
        try:
            page = await self._async_sign_in()
        except (ValueError, UnicodeError) as err:
            raise LoginStepError(STEP_PORTAL_LOGIN, f"Couldn't read the sign-in page: {type(err).__name__}") from err
        try:
            return await self._async_handoff(page)
        except (ValueError, UnicodeError) as err:
            raise LoginStepError(
                STEP_HANDOFF, f"Couldn't read a page during the hand-off: {type(err).__name__}"
            ) from err

    async def _async_sign_in(self) -> Page:
        page = await self._fetch("GET", PORTAL_START, STEP_PORTAL_LOGIN)
        if page.status == 404 or page.status >= 500:
            raise PortalUnavailable(
                STEP_PORTAL_LOGIN, f"{page.url.host} answered HTTP {page.status}", page.url.host, page.status
            )
        if page.status >= 400:
            raise LoginStepError(
                STEP_PORTAL_LOGIN, f"{page.url.host} answered HTTP {page.status}", page.url.host, page.status
            )

        form = page.login_form()
        if form is None:
            # The start page may be a landing page with a separate sign-in link.
            link = next(
                (
                    page.resolve(link.url)
                    for link in page.parsed.links
                    if _SIGN_IN_LINK.search(link.text) and is_portal_host(page.resolve(link.url))
                ),
                None,
            )
            if link is not None:
                page = await self._fetch("GET", link, STEP_PORTAL_LOGIN, referer=page.url)
                form = page.login_form()
        if _CAPTCHA.search(page.html):
            raise CaptchaRequired(
                STEP_PORTAL_LOGIN,
                "The My Washington Gas sign-in page uses a CAPTCHA, so it can't be signed in to automatically",
                page.url.host,
                page.status,
            )
        if form is None:
            scripts = ", ".join(_where(page.resolve(src)) for src in page.parsed.script_srcs[:15]) or "none"
            self._note(f"No sign-in form on {_where(page.url)}; scripts: {scripts}")
            raise LoginStepError(
                STEP_PORTAL_LOGIN,
                "The sign-in page has no HTML sign-in form (it probably signs in with JavaScript)",
                page.url.host,
                page.status,
            )

        action = page.resolve(form.action)
        self._note(f"Sign-in form posts to {_where(action)}; fields: {', '.join(form.names)}")
        if not is_portal_host(action):
            raise LoginStepError(
                STEP_PORTAL_LOGIN,
                f"The sign-in form posts to {action.host}, not washingtongas.com, so the password wasn't sent",
                action.host,
            )
        data = form.data()
        username_field = next(
            (f.name for f in form.fields if f.type in _TEXT_INPUT_TYPES and f.name and _USERNAME_FIELD.search(f.name)),
            None,
        ) or next((f.name for f in form.fields if f.type in _TEXT_INPUT_TYPES and f.name), None)
        password_field = next(f.name for f in form.fields if f.type == "password")
        if not username_field or not password_field:
            raise LoginStepError(STEP_PORTAL_LOGIN, "Couldn't find the username field", page.url.host, page.status)
        data[username_field] = self._username
        data[password_field] = self._password
        if not any(f.type == "submit" for f in form.fields):
            # ASP.NET sign-in buttons are often links that call __doPostBack.
            target = next(
                (m for link in page.parsed.links if _SIGN_IN_LINK.search(link.text) for m in [_postback(link)] if m),
                None,
            )
            if target is not None:
                data["__EVENTTARGET"], data["__EVENTARGUMENT"] = target

        result = await self._fetch("POST", action, STEP_PORTAL_LOGIN, data=data, referer=page.url)
        if result.status == 401:
            raise InvalidAuth("My Washington Gas rejected the username or password")
        if result.status >= 400:
            raise LoginStepError(
                STEP_PORTAL_LOGIN, f"Sign-in answered HTTP {result.status}", result.url.host, result.status
            )
        if is_portal_host(result.url):
            if _CAPTCHA.search(result.html) and not _CAPTCHA.search(page.html):
                raise CaptchaRequired(
                    STEP_PORTAL_LOGIN,
                    "My Washington Gas asked for a CAPTCHA after sign-in, so it can't be signed in to automatically",
                    result.url.host,
                    result.status,
                )
            if _needs_mfa(result):
                raise MfaRequired(
                    STEP_PORTAL_LOGIN,
                    "My Washington Gas asked for a verification code (multi-factor authentication), "
                    "which can't be entered automatically",
                    result.url.host,
                    result.status,
                )
            if result.login_form() is not None:
                # Only call the password wrong if the site said so on this
                # response, not in text that was already on the sign-in page.
                before = {m.group(0).lower() for m in _BAD_CREDENTIALS.finditer(page.text)}
                if any(m.group(0).lower() not in before for m in _BAD_CREDENTIALS.finditer(result.text)):
                    raise InvalidAuth("My Washington Gas rejected the username or password")
                raise LoginStepError(
                    STEP_PORTAL_LOGIN,
                    "The sign-in page came back without saying why (the site may need JavaScript to sign in)",
                    result.url.host,
                    result.status,
                )
        self._note(f"Signed in; now on {_where(result.url)}")
        self.signed_in_at = (result.url.host, result.status)
        return result

    async def _async_handoff(self, page: Page) -> OpowerHandoff:
        """Follow whatever leads from the signed-in pages to Opower."""
        queue: list[tuple[int, str, URL, dict[str, str] | None, URL | None]] = []
        seen: set[str] = set()
        last = page
        for _ in range(_MAX_HANDOFF_PAGES):
            if is_opower_host(page.url) and not _is_opower_sign_in(page.url):
                self._note(f"Reached Opower at {_where(page.url)}")
                self.handoff_at = (page.url.host, page.status)
                token = _embedded_token(page)
                return OpowerHandoff(page.url.host.split(".")[0] if page.url.host else None, token)

            sso_form = next((form for form in page.parsed.forms if form.is_sso_autosubmit()), None)
            if sso_form is not None:
                action = page.resolve(sso_form.action)
                self._note(f"SSO form ({', '.join(sso_form.names)}) posts to {_where(action)}")
                if not _is_sso_host(action):
                    raise LoginStepError(
                        STEP_HANDOFF, f"SSO form posts to unexpected host {action.host}", action.host, page.status
                    )
                last = page = await self._fetch("POST", action, STEP_HANDOFF, data=sso_form.data(), referer=page.url)
                continue

            if (is_portal_host(page.url) or is_opower_host(page.url)) and (token := _embedded_token(page)):
                self._note(f"Found an Opower access token on {_where(page.url)}")
                self.handoff_at = (page.url.host, page.status)
                subdomain = _OPOWER_HOST.search(page.html)
                return OpowerHandoff(subdomain.group(1).lower() if subdomain else None, token)

            if is_portal_host(page.url):
                for candidate in _candidates(page):
                    key = f"{candidate[2]}|{sorted(candidate[3].items()) if candidate[3] else ''}"
                    if key not in seen:
                        seen.add(key)
                        queue.append(candidate)
                queue.sort(key=lambda item: -item[0])
                if queue and len(self._trace) < 200:
                    shown = "; ".join(f"{label} ({_where(url)})" for _, label, url, _, _ in queue[:8])
                    self._note(f"Hand-off candidates: {shown}")

            if not queue:
                break
            _, label, url, data, referer = queue.pop(0)
            self._note(f"Following {label}")
            method = "POST" if data is not None else "GET"
            last = page = await self._fetch(method, url, STEP_HANDOFF, data=data, referer=referer)

        if is_opower_host(last.url):
            raise LoginStepError(
                STEP_HANDOFF,
                f"The hand-off ended on Opower's sign-in page ({_where(last.url)}), so the session wasn't passed on",
                last.url.host,
                last.status,
            )
        raise LoginStepError(
            STEP_HANDOFF,
            "Signed in to My Washington Gas, but couldn't find the link into the usage site",
            last.url.host,
            last.status,
        )


def _postback(link: _Link) -> tuple[str, str] | None:
    match = _POSTBACK.search(link.url) or _POSTBACK.search(link.onclick)
    return (match.group(1), match.group(2)) if match else None


def _score(text: str, url: URL) -> int:
    words = f"{text} {url.path}".lower()
    if _SIGN_OUT.search(words) or _is_static(url):
        return 0
    if is_opower_host(url):
        return 0 if _is_opower_sign_in(url) else 100
    if re.search(r"home\s*energy|energy\s*analysis|opower", words):
        return 60
    if re.search(r"\bsso\b|saml", words):
        return 50
    if re.search(r"usage|energy\s*use|my\s*energy", words):
        return 30
    return 0


def _candidates(page: Page) -> list[tuple[int, str, URL, dict[str, str] | None, URL | None]]:
    """Links, frames and script URLs on a signed-in page that may lead to Opower."""
    found: list[tuple[int, str, URL, dict[str, str] | None, URL | None]] = []
    parsed = page.parsed
    postback_form = next(
        (form for form in parsed.forms if "__VIEWSTATE" in form.names), parsed.forms[0] if parsed.forms else None
    )
    for link in parsed.links:
        text = " ".join(link.text.split())
        target = _postback(link)
        if target is not None:
            if postback_form is None:
                continue
            score = _score(text, URL(""))
            action = page.resolve(postback_form.action)
            if score and is_portal_host(action):
                data = postback_form.data(clicked="") | {"__EVENTTARGET": target[0], "__EVENTARGUMENT": target[1]}
                found.append((score, f'link "{text}"', action, data, page.url))
            continue
        if not link.url or link.url.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        try:
            url = page.resolve(link.url)
        except ValueError:
            continue
        if not (is_portal_host(url) or is_opower_host(url)):
            continue
        if score := _score(text, url):
            found.append((score, f'link "{text}"' if text else "link", url, None, page.url))
    for src in parsed.frames:
        try:
            url = page.resolve(src)
        except ValueError:
            continue
        if (is_portal_host(url) or is_opower_host(url)) and (score := _score("", url)):
            found.append((score, "embedded frame", url, None, page.url))
    for script in parsed.inline_scripts:
        for match in _OPOWER_URL.finditer(script.replace("\\/", "/")):
            url = URL(match.group(0).rstrip(".,;"), encoded=True)
            if score := _score("", url):
                found.append((score - 10, "Opower address in a script", url, None, page.url))
    return found


def _needs_mfa(page: Page) -> bool:
    for form in page.parsed.forms:
        for f in form.fields:
            if f.type not in ("hidden", "submit", "button", "password") and _MFA_FIELD.search(f.name):
                return True
    return page.login_form() is None and bool(_MFA_TEXT.search(page.text)) and bool(page.parsed.forms)


def _embedded_token(page: Page) -> str | None:
    """An Opower access token set in an inline script, like PSE's usage page has."""
    if "opower" not in page.html.lower():
        # Other widgets (maps, analytics) also use accessToken; only trust it next to Opower.
        return None
    scripts = page.parsed.inline_scripts
    # First choice: a token in a script that is itself about Opower.
    for script in scripts:
        if "opower" in script.lower() and (match := _EMBEDDED_TOKEN.search(script)):
            return match.group(1)
    # Then PSE's exact form, a page-level `var accessToken = "..."`.
    for script in scripts:
        if match := _PSE_TOKEN.search(script):
            return match.group(1)
    return None
