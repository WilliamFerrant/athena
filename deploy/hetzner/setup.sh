#!/usr/bin/env bash
#
# Hetzner VPS setup script for Athena
#
# Usage:
#   1. Create a Hetzner Cloud server (CX22 or higher, Ubuntu 24.04)
#   2. SSH into the server: ssh root@<your-ip>
#   3. Run: curl -sSL <this-script-url> | bash
#   Or copy this file and run: bash setup.sh
#
# Prerequisites: Fresh Ubuntu 24.04 server on Hetzner Cloud

set -euo pipefail

DOMAIN="${DOMAIN:-}"  # Set via env or change here
EMAIL="${EMAIL:-}"    # For Let's Encrypt

echo "=== Athena - Hetzner Setup ==="

# --- System updates ---
echo "[1/7] Updating system..."
apt-get update -qq && apt-get upgrade -y -qq

# --- Install Docker ---
echo "[2/7] Installing Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | bash
    systemctl enable docker
    systemctl start docker
fi

# --- Install Docker Compose plugin ---
echo "[3/7] Installing Docker Compose..."
apt-get install -y -qq docker-compose-plugin

# --- Firewall ---
echo "[4/7] Configuring firewall..."
apt-get install -y -qq ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- Clone / deploy project ---
echo "[5/7] Setting up application..."
APP_DIR="/opt/ai-companion"
mkdir -p "$APP_DIR"

if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR" && git pull
else
    echo "Copy your project files to $APP_DIR"
    echo "  scp -r ./* root@<server-ip>:$APP_DIR/"
fi

cd "$APP_DIR"

# --- SSL (Let's Encrypt) ---
echo "[6/7] Setting up SSL..."
if [ -n "$DOMAIN" ] && [ -n "$EMAIL" ]; then
    apt-get install -y -qq certbot
    certbot certonly --standalone -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive

    # Update nginx config with actual domain
    sed -i "s/yourdomain.com/$DOMAIN/g" deploy/hetzner/nginx.conf

    # Auto-renewal cron
    echo "0 3 * * * certbot renew --quiet && docker compose -f deploy/hetzner/docker-compose.prod.yml restart nginx" | crontab -
else
    echo "  Skipping SSL — set DOMAIN and EMAIL env vars for automatic setup"
    echo "  Example: DOMAIN=api.example.com EMAIL=you@example.com bash setup.sh"
fi

# --- Create .env if missing ---
if [ ! -f .env ]; then
    echo "[!] Creating .env from template — you MUST edit this with real keys"
    cp .env.example .env
    echo "  Edit: nano $APP_DIR/.env"
fi

# --- Launch ---
echo "[7/7] Starting services..."
docker compose -f deploy/hetzner/docker-compose.prod.yml up -d --build

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Application running at:"
if [ -n "$DOMAIN" ]; then
    echo "  https://$DOMAIN"
else
    echo "  http://$(curl -s ifconfig.me):80"
fi
echo ""
echo "Next steps:"
echo "  1. Edit $APP_DIR/.env with your API keys"
echo "  2. Restart: cd $APP_DIR && docker compose -f deploy/hetzner/docker-compose.prod.yml restart"
echo "  3. View logs: docker compose -f deploy/hetzner/docker-compose.prod.yml logs -f"
echo ""

