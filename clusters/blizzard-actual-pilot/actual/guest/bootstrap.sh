#!/bin/bash
set -euo pipefail

if [ -e /var/lib/actual-pilot/bootstrap.done ]; then
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
timeout 300 apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=20 update
timeout 480 apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=20 install -y \
  qemu-guest-agent podman curl e2fsprogs ufw apparmor unattended-upgrades

ufw default deny incoming
ufw default deny routed
ufw default allow outgoing
ufw allow 5006/tcp
ufw --force enable
systemctl enable --now qemu-guest-agent
systemctl enable --now apparmor
systemctl enable --now unattended-upgrades
systemctl daemon-reload
systemctl enable --now actual-pilot.service

for attempt in $(seq 1 60); do
  if curl --fail --silent --max-time 5 http://127.0.0.1:5006/ > /dev/null; then
    install -d -m 0700 /var/lib/actual-pilot
    touch /var/lib/actual-pilot/bootstrap.done
    echo 'Actual pilot bootstrap complete'
    exit 0
  fi
  echo "Waiting for Actual ($attempt/60)"
  sleep 2
done
echo 'Actual pilot did not become ready; inspect actual-pilot.service' >&2
exit 1
