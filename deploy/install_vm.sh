#!/bin/bash
# install_vm.sh — Schedule fetch_adsblol.py to run every day on the VM.
#
# systemd (Linux's service manager) runs it daily at 10:00 UTC (4 am Chicago),
# after adsb.lol has published the previous day. Each run fetches EVERY missing
# day, so if the VM was off or a download failed, the next run catches up.
# "Persistent=true" means a run missed while the VM was off happens at boot.
#
# Run once from the repo folder:   bash deploy/install_vm.sh
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

sudo tee /etc/systemd/system/ord-fetch.service > /dev/null <<UNIT
[Unit]
Description=ORD Go-Around Tracker - fetch adsb.lol daily archive
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/fetch_adsblol.py --catch-up
UNIT

sudo tee /etc/systemd/system/ord-fetch.timer > /dev/null <<UNIT
[Unit]
Description=Run the ORD archive fetch daily

[Timer]
OnCalendar=*-*-* 10:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now ord-fetch.timer
echo
echo "Installed. Next scheduled run:"
systemctl list-timers ord-fetch.timer --no-pager
