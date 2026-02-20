# Athena — Hetzner Deployment Guide

Complete guide for deploying Athena to a Hetzner Cloud VPS.

## Prerequisites

- A Hetzner Cloud account ([console.hetzner.cloud](https://console.hetzner.cloud))
- A domain name (optional but recommended for SSL)
- SSH key pair for server access

## 1. Create the Server

### Via Hetzner Console

1. Go to **Servers → Add Server**
2. **Location**: Choose closest (Falkenstein DE, Helsinki FI, Ashburn US)
3. **Image**: Ubuntu 24.04
4. **Type**: CX22 (2 vCPU, 4 GB RAM) minimum — CX32 recommended for production
5. **SSH Key**: Add your public key
6. **Name**: `athena-prod`
7. Click **Create & Buy**

### Via hcloud CLI

```bash
hcloud server create \
  --name athena-prod \
  --type cx22 \
  --image ubuntu-24.04 \
  --ssh-key your-key-name \
  --location fsn1
```

## 2. Initial Server Setup

```bash
# SSH into server
ssh root@<your-server-ip>

# Quick automated setup (recommended):
DOMAIN=athena.yourdomain.com EMAIL=you@example.com bash -c "$(curl -fsSL https://raw.githubusercontent.com/WilliamFerrant/athena/main/deploy/hetzner/setup.sh)"

# Or manual setup — see steps below
```

### Manual Setup

```bash
# System updates
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | bash
systemctl enable docker && systemctl start docker

# Install Docker Compose plugin
apt install -y docker-compose-plugin

# Firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

## 3. Deploy Application

```bash
# Clone the repository
cd /opt
git clone https://github.com/WilliamFerrant/athena.git
cd athena

# Create environment file
cp .env.example .env
nano .env   # Fill in your keys
```

### Required `.env` Configuration

```env
# Claude CLI (installed on server, or use Docker image that includes it)
CLAUDE_CLI_PATH=claude

# Optional — enables ChatGPT for the Manager agent
OPENAI_API_KEY=sk-...

# Optional — enables persistent memory
MEM0_API_KEY=m0-...

# Optional — notifications
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
TELEGRAM_BOT_TOKEN=123456:ABC-...
TELEGRAM_CHAT_ID=...
```

### Start Services

```bash
docker compose -f deploy/hetzner/docker-compose.prod.yml up -d --build
```

### Verify

```bash
# Check containers
docker compose -f deploy/hetzner/docker-compose.prod.yml ps

# Check logs
docker compose -f deploy/hetzner/docker-compose.prod.yml logs -f app

# Test the API
curl http://localhost:8000/api/status
```

## 4. SSL Certificate (Let's Encrypt)

```bash
# Install certbot
apt install -y certbot

# Get certificate (stop nginx first to free port 80)
docker compose -f deploy/hetzner/docker-compose.prod.yml stop nginx
certbot certonly --standalone -d athena.yourdomain.com --email you@example.com --agree-tos

# Update nginx config with your domain
sed -i 's/yourdomain.com/athena.yourdomain.com/g' deploy/hetzner/nginx.conf

# Restart everything
docker compose -f deploy/hetzner/docker-compose.prod.yml up -d

# Auto-renewal (cron)
echo "0 3 * * * certbot renew --quiet && docker compose -f /opt/athena/deploy/hetzner/docker-compose.prod.yml restart nginx" | crontab -
```

## 5. Reverse SSH Tunnel (Runner)

The local runner connects to the VPS through a reverse SSH tunnel:

```bash
# On your LOCAL machine (where you develop):
python -m src.runner.endpoints  # starts runner on port 7777

# Open tunnel to VPS
ssh -R 17777:localhost:7777 root@<vps-ip> -N -o ServerAliveInterval=30

# The VPS RunnerPoller auto-detects the runner through port 17777
```

### Persistent Tunnel with autossh

```bash
# Install autossh locally
# macOS: brew install autossh
# Linux: apt install autossh

autossh -M 0 -f -N \
  -R 17777:localhost:7777 \
  -o "ServerAliveInterval=30" \
  -o "ServerAliveCountMax=3" \
  root@<vps-ip>
```

## 6. Updates & Maintenance

### Deploy Update

```bash
ssh root@<vps-ip>
cd /opt/athena
git pull
docker compose -f deploy/hetzner/docker-compose.prod.yml up -d --build
```

### View Logs

```bash
# All services
docker compose -f deploy/hetzner/docker-compose.prod.yml logs -f

# Just the app
docker compose -f deploy/hetzner/docker-compose.prod.yml logs -f app

# Last 100 lines
docker compose -f deploy/hetzner/docker-compose.prod.yml logs --tail=100 app
```

### Backup SQLite Data

```bash
# Copy data directory off-server
scp -r root@<vps-ip>:/opt/athena/data ./backup-$(date +%Y%m%d)
```

### Monitor Resources

```bash
# Docker stats
docker stats

# System overview
htop
```

## 7. Cost Estimate

| Component | Spec | Monthly Cost |
|-----------|------|-------------|
| CX22 VPS | 2 vCPU, 4 GB RAM, 40 GB SSD | ~€4.51 |
| CX32 VPS | 4 vCPU, 8 GB RAM, 80 GB SSD | ~€8.49 |
| Domain | .com / .dev | ~€1/mo |
| **Total** | | **~€5-10/mo** |

## Troubleshooting

### App won't start
```bash
docker compose -f deploy/hetzner/docker-compose.prod.yml logs app
# Check for missing env vars or import errors
```

### SSL certificate issues
```bash
certbot certificates  # check status
certbot renew --dry-run  # test renewal
```

### Runner not connecting
```bash
# On VPS, check if tunnel port is open:
curl http://localhost:17777/health

# If not, re-establish the SSH tunnel from your local machine
```

### Out of memory
```bash
# Check memory usage
free -h
docker stats --no-stream

# Increase swap if needed
fallocate -l 2G /swapfile
chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```
