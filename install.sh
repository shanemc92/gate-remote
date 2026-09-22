#!/usr/bin/env bash
# Run as the user who should own the app (not root): ./install.sh
# Pass --skip-firewall to skip the ufw setup.
set -euo pipefail

SKIP_FW=false
for arg in "$@"; do
    [ "$arg" = "--skip-firewall" ] && SKIP_FW=true
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="$(whoami)"
VENV="$APP_DIR/venv"

echo "Installing gateremote as user '$APP_USER' in $APP_DIR"

python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" -q

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "Created .env from .env.example - edit it with your ntfy and RTSP details."
fi

# ffmpeg pulls the still frame from the RTSP camera, if configured
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Installing ffmpeg..."
    sudo apt-get update -qq && sudo apt-get install -y ffmpeg
fi

# RPi.GPIO needs gpio group membership to access /dev/gpiomem
sudo usermod -aG gpio "$APP_USER"

if [ "$SKIP_FW" = false ]; then
    if ! command -v ufw >/dev/null 2>&1; then
        echo "Installing ufw..."
        sudo apt-get update -qq && sudo apt-get install -y ufw
    fi

    LAN_CIDR="$(ip -o -4 addr show scope global | awk '{print $4}' | head -n1)"
    LAN_SUBNET="$(python3 -c "import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)" "$LAN_CIDR" 2>/dev/null || echo "$LAN_CIDR")"

    read -rp "LAN subnet to allow SSH from [$LAN_SUBNET]: " INPUT_SUBNET
    LAN_SUBNET="${INPUT_SUBNET:-$LAN_SUBNET}"

    read -rp "Reverse proxy IP to allow HTTP (port 4000) access from: " PROXY_IP

    sudo ufw default deny incoming
    sudo ufw default allow outgoing
    sudo ufw allow from "$LAN_SUBNET" to any port 22 proto tcp comment 'SSH LAN'
    if [ -n "$PROXY_IP" ]; then
        sudo ufw allow from "$PROXY_IP" to any port 4000 proto tcp comment 'gateremote reverse proxy'
    else
        echo "No proxy IP given - skipping port 4000 rule. Add later with:"
        echo "  sudo ufw allow from <ip> to any port 4000 proto tcp"
        echo
        echo "WARNING: port 4000 is now reachable from anywhere on the LAN."
        echo "Anyone who can reach it can toggle the gate without passing through"
        echo "the auth proxy. Add the rule above before leaving this running."
    fi
    sudo ufw --force enable
else
    echo "Skipping firewall setup (--skip-firewall)"
    echo
    echo "WARNING: no ufw rules were applied, so port 4000 is reachable from"
    echo "anywhere on the LAN and bypasses the auth proxy entirely. Restrict it"
    echo "yourself with:"
    echo "  sudo ufw allow from <proxy-ip> to any port 4000 proto tcp"
fi


sudo tee /etc/systemd/system/gateremote.service > /dev/null <<EOF
[Unit]
Description=Gate Remote Flask App
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/gunicorn -w 1 -b 0.0.0.0:4000 remote:app --access-logfile $APP_DIR/access.log --error-logfile $APP_DIR/error.log
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now gateremote.service

echo "Done. Status: sudo systemctl status gateremote"
echo "Logs:   journalctl -u gateremote -f"
