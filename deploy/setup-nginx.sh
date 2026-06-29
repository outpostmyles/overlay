#!/usr/bin/env bash
# Put Overlay behind a real URL with a login, instead of an SSH tunnel.
#
# Installs nginx as a reverse proxy in front of the app (which stays bound to 127.0.0.1:8000) and protects
# it with HTTP basic auth, so the dashboard is reachable at http://<droplet-ip> with a username + password.
# Run on the droplet as root:  bash deploy/setup-nginx.sh
set -e

echo "→ installing nginx..."
apt-get update -qq
apt-get install -y -qq nginx apache2-utils

echo
echo "Set the login for your dashboard (you'll type the password twice; it stays on the server)."
read -rp "Choose a username: " OVERLAY_USER
htpasswd -c /etc/nginx/.htpasswd "$OVERLAY_USER"      # prompts for the password, hidden

# Reverse proxy + basic auth. The single-quoted heredoc keeps $host/$remote_addr literal for nginx.
cat > /etc/nginx/sites-available/overlay <<'NGINX'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    auth_basic "Overlay";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/overlay /etc/nginx/sites-enabled/overlay
rm -f /etc/nginx/sites-enabled/default          # drop nginx's placeholder page
nginx -t
systemctl restart nginx
systemctl enable nginx >/dev/null 2>&1 || true

IP=$(curl -s -4 --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "=== DONE ==="
echo "Open  http://${IP}  and log in with the username + password you just set."
echo "(The browser may say 'Not secure' since it is plain HTTP basic auth; that is expected and it still works.)"
