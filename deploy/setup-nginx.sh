#!/usr/bin/env bash
# Put every Overlay board on one site: http://<droplet-ip>/nfl/, /cfb/, /nhl/, /mlb/ and /wc/.
#
# Installs nginx (once) as a reverse proxy in front of the boards. Each board is its own process on its
# own local port (one sport per process, same code and database), and nginx routes each address to it,
# tagging the request with X-Forwarded-Prefix so the board knows it is being served on the site. The front
# door (/) goes to the first board with games coming up. Re-run it any time; it only rewrites the config.
# Run on the droplet as root:  bash deploy/setup-nginx.sh
#
# Pair it with OVERLAY_ONE_SITE=1 in /opt/overlay/.env, so a page load on a board's old address (its own
# port, like :8003) is sent to the board's address on the site instead.
set -e

if ! command -v nginx >/dev/null 2>&1; then
    echo "→ installing nginx..."
    apt-get update -qq
    apt-get install -y -qq nginx apache2-utils
fi

# A login is OPTIONAL. Pass a username + password to require one:  setup-nginx.sh <user> <pass>
# With no arguments the site is public (no password): simplest, fine for a personal tool.
AUTH=""
OVERLAY_USER="${1:-}"
OVERLAY_PASS="${2:-}"
if [ -n "$OVERLAY_USER" ] && [ -n "$OVERLAY_PASS" ]; then
    command -v htpasswd >/dev/null 2>&1 || apt-get install -y -qq apache2-utils
    htpasswd -bc /etc/nginx/.htpasswd "$OVERLAY_USER" "$OVERLAY_PASS"
    AUTH=$'    auth_basic "Overlay";\n    auth_basic_user_file /etc/nginx/.htpasswd;'
fi

# The boards: address on the site, then the board's local port (see the systemd units in deploy/).
BOARDS="nfl:8003 cfb:8004 nhl:8002 mlb:8001 wc:8000"
LOCATIONS=""
for b in $BOARDS; do
    path="${b%%:*}"
    port="${b##*:}"
    LOCATIONS+="
    location = /${path} { return 301 /${path}/; }
    location /${path}/ {
        proxy_pass http://127.0.0.1:${port}/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-Prefix /${path};
        proxy_read_timeout 300s;
    }"
done

# Unquoted heredoc so ${AUTH} and ${LOCATIONS} expand; \$host / \$remote_addr stay literal for nginx.
cat > /etc/nginx/sites-available/overlay <<NGINX
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
${AUTH}
    # a board's snapshot is a few hundred KB of JSON; it compresses about tenfold
    gzip on;
    gzip_proxied any;
    gzip_types application/json application/javascript text/javascript text/css;

    # the front door: the first board with games coming up (any board answers it; the archive is always on)
    location = / {
        proxy_pass http://127.0.0.1:8000/go;
        proxy_set_header Host \$host;
        error_page 502 503 504 = @door;       # the archive is down: the first board in order instead
    }
    location @door { return 302 /nfl/; }
    # the page carries its own icon; browsers still ask the site root for one
    location = /favicon.ico { access_log off; log_not_found off; return 204; }
    # anything else (a mistyped board, an old link) goes to the front door
    location / { return 302 /; }
${LOCATIONS}
}
NGINX

ln -sf /etc/nginx/sites-available/overlay /etc/nginx/sites-enabled/overlay
rm -f /etc/nginx/sites-enabled/default          # drop nginx's placeholder page
nginx -t
systemctl reload nginx 2>/dev/null || systemctl restart nginx
systemctl enable nginx >/dev/null 2>&1 || true

IP=$(curl -s -4 --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "=== DONE ==="
echo "Open  http://${IP}/  (every board: /nfl/ /cfb/ /nhl/ /mlb/ /wc/)."
[ -n "$AUTH" ] && echo "Log in with the username + password you just set (plain HTTP basic auth, so the browser may say 'Not secure')."
exit 0
