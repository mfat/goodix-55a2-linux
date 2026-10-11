#!/bin/bash
# Removes a source install, its templates and the prints made with this driver. Run as root.
set -euo pipefail
cd "$(dirname "$0")"

systemctl disable --now goodix55a2-bridge.service || true
make uninstall TOD_CFLAGS= TOD_LIBS=
rm -rf /run/goodix55a2 /var/lib/goodix55a2 /var/lib/fprint/*/goodix55a2
systemctl daemon-reload
systemctl try-restart fprintd.service || true
echo "Removed."
