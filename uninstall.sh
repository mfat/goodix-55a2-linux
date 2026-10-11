#!/bin/bash
# Removes the Goodix 27c6:55a2 TOD driver + bridge and its templates. Run as root.
set -euo pipefail
TOD_DIR=$(pkg-config --variable=tod_driversdir libfprint-2-tod-1 2>/dev/null || true)
[ -n "$TOD_DIR" ] || TOD_DIR=/usr/lib64/libfprint-2/tod-1

systemctl disable --now goodix55a2-bridge.service || true
rm -f /usr/local/libexec/goodix55a2-bridge /etc/systemd/system/goodix55a2-bridge.service \
      /etc/tmpfiles.d/goodix55a2.conf "$TOD_DIR/libfprint-tod-goodix55a2.so"
rm -rf /run/goodix55a2 /var/lib/goodix55a2 /var/lib/fprint/*/goodix55a2
systemctl daemon-reload
systemctl stop fprintd || true
echo "Removed."
