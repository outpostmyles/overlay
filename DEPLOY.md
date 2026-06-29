# Deploying Overlay (always-on)

Overlay is built to run on your laptop, but the **Model Ledger** needs the server up around the clock:
it locks each game's forecast about 75 minutes before kickoff and settles it after the result posts, and
both only happen when the app refreshes. With no browser open, nothing refreshes, so a forecast misses its
lock window and a finished game never grades. Running it on a small always-on host fixes that.

This guide uses a **DigitalOcean Droplet**. Any always-on Linux box (or another VPS) works the same way.

## Why a Droplet, not App Platform

The whole point of the app is the record that **accumulates on disk**: the SQLite ledger (`poly.db`), the
frozen group field, logged leans, and the growing forecast table. App Platform (and most "serverless"
hosts) use an **ephemeral filesystem** that resets on every deploy or restart, which would wipe that
history. A Droplet keeps a normal persistent disk, so the track record survives. Smallest Ubuntu Droplet
is plenty (the app is light and spends nothing on the free feeds).

## The two pieces

1. **systemd** keeps the `uvicorn` process running and restarts it on crash or reboot.
2. **The in-process heartbeat** (already in the code) refreshes the free feeds on a timer so the Model
   Ledger ticks with no browser open. Enable it with `HEARTBEAT_ENABLED=true` in `.env`.

The heartbeat runs the **free path only** (`build_snapshot(force=True)` with `refresh_odds=False` and
`reason=False`), so it pulls Polymarket / Kalshi / ESPN and **never spends Odds credits or Anthropic
tokens**. It is also gentler than the 60-second browser poll you already run with a tab open. The Odds and
Analyze buttons stay manual, exactly as on your laptop.

## Cost: zero by default

The recommended server setup is **no API keys in `.env`** (the default state of `.env.example`). That gives
a hard guarantee of $0 spend and still runs the full free core: the Model Ledger (the reason for an
always-on host), the de-vigged Best Bets picks, the Futures bracket, and moneyline CLV all run on the free
Polymarket / Kalshi / ESPN feeds. The keys only add optional polish that goes dormant without them:

- `ANTHROPIC_API_KEY` blank: no AI match verdicts or per-prop "Read" commentary.
- `ODDS_API_KEY` blank: no sportsbook best-price / EV line-shopping column (you keep the sharp fair line).
- `APIFOOTBALL_KEY` blank: shots / passes / corner props do not auto-grade (moneyline and goalscorer still
  grade for free).

You can add any key to the server `.env` later and `systemctl restart overlay`. Note that even with the
keys set, nothing on the 24/7 loop auto-spends: Anthropic and the Odds API only spend on a manual button
press, and API-Football is the free tier. Leaving them blank is simply belt-and-suspenders.

## Setup

```bash
# 1. On the Droplet (Ubuntu), as root or with sudo:
adduser --system --group --home /opt/overlay overlay
apt update && apt install -y python3-venv git

# 2. Get the code and build the venv (run.sh creates .venv and installs requirements):
cd /opt && git clone https://github.com/outpostmyles/overlay.git overlay
chown -R overlay:overlay /opt/overlay
sudo -u overlay bash -c 'cd /opt/overlay && python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt'

# 3. Configure. Copy the template and turn the heartbeat ON. Leave all the API keys BLANK (the template's
#    default) for a guaranteed-free, fully-working server; you can add keys later if you want the extras.
sudo -u overlay cp /opt/overlay/.env.example /opt/overlay/.env
sudo -u overlay sed -i 's/^HEARTBEAT_ENABLED=.*/HEARTBEAT_ENABLED=true/' /opt/overlay/.env

# 4. Install the service (the unit file ships in the repo at deploy/overlay.service):
cp /opt/overlay/deploy/overlay.service /etc/systemd/system/overlay.service
systemctl daemon-reload
systemctl enable --now overlay

# 5. Confirm it is up and the heartbeat is ticking:
systemctl status overlay
journalctl -u overlay -f      # look for "[heartbeat] enabled..." then periodic "[aggregator] ..." lines
```

## Reaching it (there is no built-in auth)

The service binds to `127.0.0.1`, so it is not exposed to the internet by default. Do **not** change it to
`--host 0.0.0.0` without putting auth in front: the dashboard has no login. Two good options:

- **SSH tunnel (simplest).** From your laptop:
  `ssh -L 8000:127.0.0.1:8000 overlay@YOUR_DROPLET_IP` then open `http://localhost:8000`. Nothing is
  public; you see it only while the tunnel is open. The heartbeat keeps the data fresh regardless.
- **nginx + HTTP basic auth + TLS.** Reverse-proxy a domain to `127.0.0.1:8000`, add a basic-auth
  password (`htpasswd`), and get a free certificate with `certbot`. Use this if you want a real URL.

A `ufw` firewall allowing only SSH (22) and, if you use nginx, HTTPS (443) is a sensible default.

## Keeping data safe

`poly.db` and the `poly_*.json` caches in `/opt/overlay` are your history. They are gitignored (never
committed) and live only on the Droplet, so back them up if they matter to you:

```bash
sudo -u overlay cp /opt/overlay/poly.db /opt/overlay/backups/poly-$(date +%F).db   # e.g. via a daily cron
```

## Updating

```bash
cd /opt/overlay && sudo -u overlay git pull
sudo -u overlay .venv/bin/pip install -q -r requirements.txt   # only if requirements changed
systemctl restart overlay
```

The new forecast table is created automatically on startup, and the ledger is forward-only, so updates
never rewrite past forecasts.

## Tuning

- `HEARTBEAT_INTERVAL_SECONDS` (default `300`) controls the refresh cadence. Five minutes comfortably
  catches every 75-minute lock window and settles finished games promptly; there is no benefit to going
  below a minute (the floor), and the free feeds are cached anyway.
- To pause the server-side ticking without stopping the app, set `HEARTBEAT_ENABLED=false` and restart.

## Alternative: external cron instead of the in-process heartbeat

If you would rather not run the in-process loop, leave `HEARTBEAT_ENABLED=false` and have cron poke the
endpoint on the same free path:

```cron
*/5 * * * * curl -fs "http://127.0.0.1:8000/api/snapshot?force=true" >/dev/null
```

Both approaches do the same work; the in-process heartbeat is just self-contained and needs no crontab.
