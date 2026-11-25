#!/bin/bash
# setup_detect_vm.sh — Prepare the VM to run detect.py.
#
#   1. Adds 2 GB of swap (overflow memory on disk). The free VM has only 1 GB of
#      RAM, and loading a day of positions into pandas can briefly need more.
#   2. Installs the detection libraries (traffic, pandas 2.x, matplotlib).
#
# Run once from the repo folder:   bash deploy/setup_detect_vm.sh
# Then re-run:                      bash deploy/install_vm.sh
set -e

if ! swapon --show | grep -q /swapfile; then
  echo "Adding 2 GB swap..."
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile > /dev/null
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
fi

echo "Installing detection libraries (takes a few minutes)..."
sudo apt-get install -y -q python3-pip > /dev/null
pip3 install --user --break-system-packages -q "pandas>=2.2,<3" "traffic>=2.13" "matplotlib>=3.8"

python3 -c "import traffic, pandas; print('OK: traffic', traffic.__version__, '/ pandas', pandas.__version__)"
