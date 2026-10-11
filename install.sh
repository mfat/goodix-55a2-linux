#!/bin/bash
# Installs the Goodix 27c6:55a2 TOD driver + bridge for fprintd. Run as root after tod/build.sh.
set -euo pipefail
cd "$(dirname "$0")"

TOD_DIR=$(pkg-config --variable=tod_driversdir libfprint-2-tod-1 2>/dev/null || true)
[ -n "$TOD_DIR" ] || TOD_DIR=/usr/lib64/libfprint-2/tod-1
[ -f tod/libfprint-tod-goodix55a2.so ] || { echo "run tod/build.sh first" >&2; exit 1; }

install -Dm755 bridge/goodix55a2-bridge.py /usr/local/libexec/goodix55a2-bridge
install -Dm644 systemd/goodix55a2-bridge.service /etc/systemd/system/goodix55a2-bridge.service
install -Dm644 systemd/goodix55a2.tmpfiles /etc/tmpfiles.d/goodix55a2.conf
install -Dm644 tod/libfprint-tod-goodix55a2.so "$TOD_DIR/libfprint-tod-goodix55a2.so"

systemd-tmpfiles --create /etc/tmpfiles.d/goodix55a2.conf
systemctl daemon-reload
systemctl stop fprintd || true
systemctl enable goodix55a2-bridge.service
systemctl restart goodix55a2-bridge.service
echo "Installed. Enroll with: fprintd-enroll"
systemctl --no-pager -n3 status goodix55a2-bridge | tail -4
