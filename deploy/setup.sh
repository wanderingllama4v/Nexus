#!/bin/bash
# deploy/setup.sh — Bootstrap NEXUS on EC2 (Ubuntu 22.04+)
# Run once as the ubuntu user: bash deploy/setup.sh

set -e

NEXUS_DIR="$HOME/Nexus"
VENV_DIR="$NEXUS_DIR/venv"
DB_NAME="nexus"
DB_USER="nexus"
DB_PASS="nexus"   # change before running in production

echo "=== NEXUS EC2 setup ==="

# ── System packages ───────────────────────────────────────────────────────────
sudo apt-get update -qq
sudo apt-get install -y software-properties-common curl git libpq-dev \
    postgresql postgresql-contrib

# Add deadsnakes PPA if python3.11 not already present
if ! command -v python3.11 &>/dev/null; then
    echo "--- Adding deadsnakes PPA for Python 3.11"
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -qq
    sudo apt-get install -y python3.11 python3.11-venv python3.11-dev
else
    echo "--- Python 3.11 already installed: $(python3.11 --version)"
fi

# ── PostgreSQL ────────────────────────────────────────────────────────────────
echo "--- Setting up PostgreSQL"
sudo systemctl enable postgresql
sudo systemctl start postgresql

sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || true
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" 2>/dev/null || true

echo "PostgreSQL: database '$DB_NAME' ready"

# ── Python venv ───────────────────────────────────────────────────────────────
echo "--- Creating Python venv"
python3.11 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$NEXUS_DIR/requirements.txt" -q

echo "Python venv ready"

# ── .env file ─────────────────────────────────────────────────────────────────
if [ ! -f "$NEXUS_DIR/.env" ]; then
    cp "$NEXUS_DIR/.env.example" "$NEXUS_DIR/.env"
    echo ""
    echo "⚠️  .env created from .env.example — fill in your credentials:"
    echo "    nano $NEXUS_DIR/.env"
fi

# ── Initialise DB schema ──────────────────────────────────────────────────────
echo "--- Initialising DB schema"
cd "$NEXUS_DIR"
"$VENV_DIR/bin/python" nexus.py --init-db

# ── systemd service ───────────────────────────────────────────────────────────
echo "--- Installing systemd service"
sudo cp "$NEXUS_DIR/deploy/nexus.service" /etc/systemd/system/nexus.service
sudo sed -i "s|__HOME__|$HOME|g" /etc/systemd/system/nexus.service
sudo sed -i "s|__USER__|$USER|g" /etc/systemd/system/nexus.service
sudo systemctl daemon-reload
sudo systemctl enable nexus

echo ""
echo "=== Setup complete ==="
echo "Start the service:  sudo systemctl start nexus"
echo "View logs:          journalctl -u nexus -f"
echo "Dashboard:          http://$(curl -s ifconfig.me):8001"
