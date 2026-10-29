#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu server (for example an Oracle Cloud "Always Free" VM).
# Installs Docker, opens ports 80/443 in the server firewall, clones the app, creates
# deploy/oracle/.env with fresh random secrets, and starts everything. See DEPLOY-ORACLE.md.
#
#   bash setup.sh                    # address becomes <public-ip>.sslip.io
#   bash setup.sh app.example.com    # your own domain (its DNS must point at this server)
#
# Safe to re-run: an existing .env is kept, so secrets and data survive.
set -euo pipefail

REPO_URL="https://github.com/shreyasvishwa1/ai-software-engineer-codebase.git"
APP_DIR="$HOME/ai-software-engineer-codebase"
SITE_ADDRESS="${1:-}"

echo "==> Installing Docker and Git"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 git curl openssl
sudo systemctl enable --now docker

echo "==> Opening ports 80 and 443 in the server firewall"
# Oracle's Ubuntu images reject everything except SSH in iptables, in addition to the cloud
# security list (which must allow 80/443 too; see DEPLOY-ORACLE.md).
for rule in "-p tcp --dport 80" "-p tcp --dport 443" "-p udp --dport 443"; do
  # shellcheck disable=SC2086
  sudo iptables -C INPUT -m state --state NEW $rule -j ACCEPT 2>/dev/null ||
    sudo iptables -I INPUT 1 -m state --state NEW $rule -j ACCEPT
done
if command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save
else
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
  sudo netfilter-persistent save
fi

# Small servers need swap to build the frontend.
if [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 4000000 ] && ! swapon --show | grep -q .; then
  echo "==> Adding 4 GB of swap"
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> Getting the app"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR/deploy/oracle"

if [ ! -f .env ]; then
  if [ -z "$SITE_ADDRESS" ]; then
    SITE_ADDRESS="$(curl -fsS https://api.ipify.org).sslip.io"
  fi
  echo "==> Creating .env for $SITE_ADDRESS"
  umask 077
  cat > .env <<EOF
SITE_ADDRESS=$SITE_ADDRESS
POSTGRES_PASSWORD=$(openssl rand -hex 24)
SECRET_KEY=$(openssl rand -hex 32)

# Optional: free AI explanations with Google Gemini (key from https://aistudio.google.com/apikey)
GEMINI_API_KEY=

# Optional: password reset emails through Gmail (use a Google app password)
SMTP_HOST=
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
EOF
else
  echo "==> Keeping the existing .env"
  SITE_ADDRESS="$(grep -E '^SITE_ADDRESS=' .env | cut -d= -f2-)"
fi

echo "==> Building and starting (the first build takes several minutes)"
sudo docker compose up -d --build

echo "==> Waiting for the site"
for _ in $(seq 1 60); do
  if curl -fsS "https://$SITE_ADDRESS/health" >/dev/null 2>&1; then
    echo
    echo "Ready: https://$SITE_ADDRESS"
    exit 0
  fi
  sleep 5
done
echo "Started, but https://$SITE_ADDRESS is not answering yet."
echo "Check that the Oracle security list allows TCP 80 and 443, then: sudo docker compose logs edge app"
exit 1
