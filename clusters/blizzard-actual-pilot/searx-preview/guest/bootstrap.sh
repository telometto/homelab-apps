#!/bin/bash
set -euo pipefail

if [ -e /var/lib/searx-preview/bootstrap.done ]; then
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
timeout 300 apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=20 update
timeout 480 apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=20 install -y \
  qemu-guest-agent podman curl valkey-server ufw apparmor unattended-upgrades

ufw default deny incoming
ufw default deny routed
ufw default allow outgoing
ufw allow 8080/tcp
ufw --force enable
systemctl enable --now qemu-guest-agent apparmor unattended-upgrades
# Use a dedicated loopback-only, disposable cache instead of distro persistence.
systemctl disable --now valkey-server.service
systemctl daemon-reload
systemctl enable --now searx-preview.service

for attempt in $(seq 1 60); do
  if curl --fail --silent --max-time 5 http://127.0.0.1:8080/healthz > /dev/null; then
    install -d -m 0700 /var/lib/searx-preview
    touch /var/lib/searx-preview/bootstrap.done
    echo 'SearxNG preview bootstrap complete'
    exit 0
  fi
  echo "Waiting for SearxNG ($attempt/60)"
  sleep 2
done
echo 'SearxNG did not become ready; inspect searx-preview.service' >&2
exit 1
