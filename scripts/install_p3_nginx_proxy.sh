#!/usr/bin/env bash
set -Eeuo pipefail

SERVER_NAME="${1:-_}"
UPSTREAM="${P3_PROXY_UPSTREAM:-http://127.0.0.1:8093}"
SITE_NAME="${P3_PROXY_SITE_NAME:-direction-engine-p3}"
SITE_AVAILABLE="/etc/nginx/sites-available/${SITE_NAME}"
SITE_ENABLED="/etc/nginx/sites-enabled/${SITE_NAME}"

if [[ "$EUID" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

if ! command -v nginx >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update
    $SUDO apt-get install -y nginx
  else
    echo "ERROR: nginx is missing and apt-get is unavailable" >&2
    exit 1
  fi
fi

if ! curl -fsS --connect-timeout 1 --max-time 3 "${UPSTREAM}/health" >/tmp/p3-proxy-upstream-health.json; then
  echo "ERROR: P3 upstream is not healthy: ${UPSTREAM}/health" >&2
  exit 1
fi

$SUDO tee "$SITE_AVAILABLE" >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${SERVER_NAME};

    client_max_body_size 2m;

    location / {
        proxy_pass ${UPSTREAM};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 60s;
        proxy_send_timeout 60s;
    }
}
EOF

$SUDO ln -sfn "$SITE_AVAILABLE" "$SITE_ENABLED"
$SUDO nginx -t
$SUDO systemctl enable nginx >/dev/null 2>&1 || true
$SUDO systemctl reload nginx || $SUDO systemctl restart nginx

echo "=== PROXY HEALTH ==="
curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1/health
echo
echo "P3_NGINX_PROXY_PASS | public=http://SERVER_IP/ | upstream=${UPSTREAM}"
