#!/bin/bash
# install_vm.sh — Turn collector.py into a background service on the VM.
#
# systemd (Linux's service manager) will then:
#   * start the collector automatically when the VM boots
#   * restart it 30 s after any crash
#   * stop it cleanly on shutdown
#
# Run once from the repo folder:   bash deploy/install_vm.sh
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$REPO_DIR/credentials.json" ]; then
  echo "credentials.json is missing from $REPO_DIR. Copy it there first."
  exit 1
fi
chmod 600 "$REPO_DIR/credentials.json"   # only your user can read the secret

sudo tee /etc/systemd/system/ord-collector.service > /dev/null <<EOF
[Unit]
Description=ORD Go-Around Tracker - OpenSky collector
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/collector.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ord-collector
echo
echo "Installed. Current status:"
systemctl status ord-collector --no-pager --lines=5
