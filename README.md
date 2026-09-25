# Washington Gas for Home Assistant

See your Washington Gas usage and cost in Home Assistant, including the Energy dashboard. It's built for homes that heat with gas, so you can see how much a cold week is actually costing you.

It works the same way as Home Assistant's built-in Opower integration (the one BGE, Pepco and others use). Washington Gas runs its usage site on Opower at [wgl.opower.com](https://wgl.opower.com), but it isn't in the built-in integration's list, so this adds it.

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

## Before you install: get a wgl.opower.com login

The integration signs in to [wgl.opower.com](https://wgl.opower.com), Washington Gas's Home Energy Analysis site, where the usage data lives. That site has its own logins, separate from My Washington Gas ([my.washingtongas.com](https://my.washingtongas.com)).

If you've only ever used My Washington Gas, make a wgl.opower.com login first:

1. Go to [wgl.opower.com/ei/x/create-account](https://wgl.opower.com/ei/x/create-account).
2. Enter your Washington Gas account number (it's on your bill), your name, an email address and a password.
3. Check that you can sign in at [wgl.opower.com](https://wgl.opower.com) and see your usage.

Use that email and password when you set up the integration.

### Why not the My Washington Gas login?

My Washington Gas signs you in from JavaScript, and it sends a Google reCAPTCHA token along with your password. That CAPTCHA is there to block automated sign-ins, and this integration won't try to get around it. So the integration can't sign in at my.washingtongas.com, and your My Washington Gas password won't work on wgl.opower.com either, since the two sites keep separate logins.

### Test it before installing

You can check your login from any computer with Python, or from inside your Home Assistant container, before setting anything up:

```bash
git clone https://github.com/nkirchoff/Washington-gas-home-assisted-plug-in
cd Washington-gas-home-assisted-plug-in
pip install aiohttp   # not needed inside the Home Assistant container
python3 scripts/check_connection.py
```

It asks for your wgl.opower.com email and password (it doesn't save them) and goes through the sign-in one step at a time:

1. **Sign-in**: signing in at wgl.opower.com (and at wglm.opower.com, Washington Gas's second Opower site, if the first one doesn't know your login)
2. **Reading accounts**: reading your gas accounts from the site

For each step it prints OK or FAILED, plus the site and HTTP status where it stopped. It never prints passwords, cookies or tokens, and account numbers are partly hidden, so you can paste the whole output into a GitHub issue.

When it works, it shows your accounts, the current bill, the last 10 days and your recent bills.

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
2. Enter your wgl.opower.com email and password (see "get a wgl.opower.com login" above).
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
- Your password is only sent while signing in, and only to Washington Gas's Opower sites: wgl.opower.com, or wglm.opower.com if wgl.opower.com doesn't know your login. It never goes to my.washingtongas.com or anywhere else.

## Troubleshooting

**"wgl.opower.com didn't accept that email and password"**: make sure you can sign in at [wgl.opower.com](https://wgl.opower.com) with them. A My Washington Gas login won't work there; see "Before you install" above.

**"Couldn't sign in to wgl.opower.com"**: try again in a few minutes. If it keeps happening, run `scripts/check_connection.py` (see "Test it before installing" above) and open an issue with its output. It shows exactly which step stopped and where.

**No daily sensors, only bill sensors**: your meter only reports once per bill, so daily data isn't available for your account. Bill history still goes into the Energy dashboard.

**Something else isn't right**: turn on debug logging by adding this to `configuration.yaml` and restarting:

```yaml
logger:
  logs:
    custom_components.washington_gas: debug
```

Then check **Settings > System > Logs**. You can also download diagnostics from the integration's page (three dots > **Download diagnostics**). Passwords and account numbers are removed from it. Open an issue with the log lines or the diagnostics file.

## Status

This is a new integration and **hasn't been tried with a real Washington Gas account yet.** Here's what has been checked against the real sites:

- my.washingtongas.com's sign-in page and scripts were read directly. The sign-in is a JavaScript call that sends a Google reCAPTCHA token with the password, and the page won't send it without one. That's why the integration doesn't use that login.
- wgl.opower.com's own sign-in form posts to the same address the integration uses, with no CAPTCHA. Its create-account page asks for an account number, name, email and password.
- The data requests (accounts, reads and bill forecasts) follow the [opower](https://github.com/tronikos/opower) library, which Home Assistant's built-in Opower integration uses for many other utilities on the same Opower platform.

If something doesn't work, the check script shows which step stopped and where.

## Credits

The request formats come from the [opower](https://github.com/tronikos/opower) library by tronikos (Apache 2.0), which powers Home Assistant's built-in Opower integration. This integration doesn't depend on it, so it can't conflict with the built-in one.

## Development

```bash
pip install -r requirements_test.txt ruff
ruff check . && ruff format --check .
pytest
```

Tests run the real integration code inside Home Assistant against a fake Opower site in `tests/fake_opower.py`.
