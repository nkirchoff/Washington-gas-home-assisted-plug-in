# Washington Gas for Home Assistant

See your Washington Gas usage and cost in Home Assistant, including the Energy dashboard. It's built for homes that heat with gas, so you can see how much a cold week is actually costing you.

It works the same way as Home Assistant's built-in Opower integration (the one Dominion, BGE, Pepco and others use). Washington Gas runs its usage site on Opower at [wgl.opower.com](https://wgl.opower.com), but it isn't in the built-in integration's list, so this adds it.

## What you get

**Energy dashboard history.** Your gas usage and cost get imported as long-term statistics:

- Up to about 3 years of daily usage, if your meter reports daily
- Monthly bill totals for anything older than that
- New days added automatically, usually 1 to 2 days after they happen

**Sensors** for each account:

| Sensor | What it shows |
| --- | --- |
| Current bill gas usage / cost | Usage and cost so far in the current billing period |
| Forecasted bill gas usage / cost | What Washington Gas expects this bill to come to |
| Typical bill gas usage / cost | The same period last year, for comparison |
| Latest daily gas usage / cost | The newest full day Washington Gas has published |
| Last bill gas usage / cost | Your most recent completed bill |
| Bill period start / end | Dates of the current billing period |

The forecast sensors only show up if Washington Gas provides a forecast for your account, and the daily sensors only show up if your meter reports daily.

## Before you install: check your login

Use the email and password you use at [my.washingtongas.com](https://my.washingtongas.com) (My Washington Gas). That's the only login most people have, and it's what the integration tries first.

The usage data itself lives on Washington Gas's Home Energy Analysis site at [wgl.opower.com](https://wgl.opower.com), which is run by Opower. Your My Washington Gas login usually won't work if you type it into wgl.opower.com's own sign-in page. That's expected. The integration signs in at my.washingtongas.com and then follows the site's own link into the usage pages, the same way your browser does when you click through to your usage.

If you happen to have a separate account that you made on wgl.opower.com itself, that works too. The integration falls back to it when the My Washington Gas route doesn't work.

### Test it before installing

You can check your login from any computer with Python, or from inside your Home Assistant container, before setting anything up:

```bash
git clone https://github.com/nkirchoff/Washington-gas-home-assisted-plug-in
cd Washington-gas-home-assisted-plug-in
pip install aiohttp   # not needed inside the Home Assistant container
python3 scripts/check_connection.py
```

It asks for your email and password (it doesn't save them) and goes through the sign-in one step at a time:

1. **My Washington Gas sign-in**: logging in at my.washingtongas.com
2. **Hand-off to Opower**: following the link from My Washington Gas into the usage site
3. **Opower API**: reading your accounts from the usage site

For each step it prints OK or FAILED, plus the site and HTTP status where it stopped. If something fails, it also lists each request it made (just the site, the page path and the status). It never prints passwords, cookies, tokens or the parts of addresses that can carry them, and account numbers are partly hidden, so you can paste the whole output into a GitHub issue. Add `--trace` to see the request list even when everything works.

When it works, it shows your accounts, the current bill, the last 10 days and your recent bills.

### Verification codes and CAPTCHAs

If My Washington Gas asks for a verification code (multi-factor authentication) or shows a CAPTCHA when signing in, the integration stops there and says so. Both the check script and the Home Assistant setup screen report it. It can't enter codes for you, and it won't try to get around a CAPTCHA. If that's what you see, the integration can't sign in to your account automatically.

## Install

### With HACS (recommended)

1. In Home Assistant, open **HACS**.
2. Open the menu (three dots, top right) and pick **Custom repositories**.
3. Paste `https://github.com/nkirchoff/Washington-gas-home-assisted-plug-in`, choose **Integration** as the type, and click **Add**.
4. Search HACS for **Washington Gas**, open it, and click **Download**.
5. Restart Home Assistant.

### By hand

1. Copy the `custom_components/washington_gas` folder into your Home Assistant `config/custom_components/` folder.
2. Restart Home Assistant.

## Set it up

1. Go to **Settings > Devices & services > Add integration** and search for **Washington Gas**.
2. Enter your My Washington Gas email and password (see "check your login" above).
3. Pick the unit for the Energy dashboard:
   - **Therms** (default): the same numbers as your bill. Home Assistant has no therm unit, so the Energy dashboard labels them CCF. This is exactly what the built-in Opower integration does.
   - **kWh**: therms converted exactly (1 therm = 29.3071 kWh). Handy if you want to compare gas and electricity on the same scale.

   You can't switch this later without removing and re-adding the integration, since it changes how history is stored.

The first update pulls in your history, which can take a minute.

### Add it to the Energy dashboard

1. Go to **Settings > Dashboards > Energy**.
2. Under **Gas consumption**, click **Add gas source**.
3. Pick **Washington Gas (your account number) gas usage**.
4. For cost, choose **Use an entity tracking the total costs** and pick **Washington Gas (your account number) gas cost**.
5. Save.

Give it a few minutes, then open the Energy dashboard and switch to a past day, week or month. Today's usage usually won't show yet because Washington Gas publishes each day a day or two later.

## How it works

- It checks Washington Gas every 12 hours. Their data only updates once a day and runs 1 to 2 days behind, so checking more often wouldn't give you anything newer.
- It signs in fresh each time because the site's logins expire after a few minutes.
- If your password changes, Home Assistant will ask you for the new one.
- It signs in the way it worked the first time (through My Washington Gas, or directly on Opower) and sticks with that.
- Your password is only sent while signing in, and only to washingtongas.com and Washington Gas's own Opower sign-in (wgl.opower.com and wglm.opower.com, used for the fallback). Everything stays between your Home Assistant, Washington Gas and its Opower site.

## Troubleshooting

**"Washington Gas didn't accept that email and password"**: make sure they work at [my.washingtongas.com](https://my.washingtongas.com).

**"Signed in to My Washington Gas, but couldn't get from there to the usage site"** or **"Couldn't sign in"**: run `scripts/check_connection.py` (see "Test it before installing" above) and open an issue with its output. It shows exactly which step stopped and where.

**"My Washington Gas asked for a verification code"** or **"is showing a CAPTCHA"**: see "Verification codes and CAPTCHAs" above.

**No daily sensors, only bill sensors**: your meter only reports once per bill, so daily data isn't available for your account. Bill history still goes into the Energy dashboard.

**Something else isn't right**: turn on debug logging by adding this to `configuration.yaml` and restarting:

```yaml
logger:
  logs:
    custom_components.washington_gas: debug
```

Then check **Settings > System > Logs**. You can also download diagnostics from the integration's page (three dots > **Download diagnostics**). Passwords and account numbers are removed from it. Open an issue with the log lines or the diagnostics file.

## Status

This is a new integration, and **the My Washington Gas route hasn't been confirmed against the real site yet.** The Washington Gas site doesn't publish any docs for this. It also couldn't be loaded from the environment this was built in, so its sign-in pages and scripts haven't been inspected directly. Instead, the sign-in follows whatever the site does, step by step, using the same approach the [opower](https://github.com/tronikos/opower) library uses for utilities whose login goes through their own site first (AES Indiana and Puget Sound Energy in particular). That covers regular sign-in forms (including ASP.NET ones), redirects, SAML hand-off forms, ASP.NET postback links, and an Opower token embedded in the usage page.

If Washington Gas does it some other way, the check script will stop at a named step and show the requests it made. That's enough to add the missing piece. Everything is covered by automated tests against a simulated My Washington Gas site and a simulated Opower site.

## Credits

The request formats come from the [opower](https://github.com/tronikos/opower) library by tronikos (Apache 2.0), which powers Home Assistant's built-in Opower integration. This integration doesn't depend on it, so it can't conflict with the built-in one.

## Development

```bash
pip install -r requirements_test.txt ruff
ruff check . && ruff format --check .
pytest
```

Tests run the real integration code inside Home Assistant against fake My Washington Gas and Opower sites in `tests/fake_opower.py`.
