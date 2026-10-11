#!/bin/bash
# Builds and installs from source. Prefer the distro packages if available.
# Run as root (needs the libfprint-tod development files, see README).
set -euo pipefail
cd "$(dirname "$0")"

make
make install
systemd-tmpfiles --create /usr/lib/tmpfiles.d/goodix55a2.conf
systemctl daemon-reload
systemctl enable goodix55a2-bridge.service
systemctl restart goodix55a2-bridge.service
systemctl try-restart fprintd.service || true
echo "Installed. Enroll with: fprintd-enroll"
