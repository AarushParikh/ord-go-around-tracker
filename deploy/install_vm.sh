#!/bin/bash
# install_vm.sh — Schedule the daily pipeline on the VM:
#   1. fetch_adsblol.py  downloads every missing day of ORD positions
#   2. detect.py         finds go-arounds and counts arrivals in every new day
#   3. weather.py        attaches the METAR in effect at each new go-around
#   4. classify.py       infers the likely contributing factor (rules)
#   5. publish_snapshot  pushes data/tracker.db to GitHub -> dashboard updates
#
# systemd (Linux's service manager) runs it daily at 10:00 UTC (4 am Chicago),
# after adsb.lol has published the previous day. Each step processes EVERY
# missing day, so if the VM was off or a step failed, the next run catches up.
# (detect.py needs deploy/setup_detect_vm.sh to have been run once.)
# "Persistent=true" means a run missed while the VM was off happens at boot.
#
# Run once from the repo folder:   bash deploy/install_vm.sh
set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

sudo tee /etc/systemd/system/ord-fetch.service > /dev/null <<UNIT
[Unit]
Description=ORD Go-Around Tracker - fetch adsb.lol archive, then detect go-arounds
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 $REPO_DIR/fetch_adsblol.py --catch-up
ExecStart=/bin/sh -c '/usr/bin/python3 -u $REPO_DIR/detect.py >> $REPO_DIR/logs/detect.log 2>&1'
ExecStart=/bin/sh -c '/usr/bin/python3 -u $REPO_DIR/weather.py >> $REPO_DIR/logs/detect.log 2>&1'
ExecStart=/bin/sh -c '/usr/bin/python3 -u $REPO_DIR/classify.py >> $REPO_DIR/logs/detect.log 2>&1'
ExecStart=/bin/sh -c '/bin/bash $REPO_DIR/deploy/publish_snapshot.sh >> $REPO_DIR/logs/detect.log 2>&1'
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
