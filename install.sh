#!/bin/bash
# Builds and installs from source. Prefer the distro packages if available.
# Run as root (needs the libfprint-tod development files, see README).
set -euo pipefail
cd "$(dirname "$0")"

make
make install
# Fedora/RHEL: fprintd is confined by SELinux and needs a policy to reach the bridge socket.
if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; then
    make selinux install-selinux
    /usr/libexec/goodix55a2-selinux install /usr/share/selinux/packages/goodix55a2.pp
fi
systemd-tmpfiles --create /usr/lib/tmpfiles.d/goodix55a2.conf
systemctl daemon-reload
systemctl enable goodix55a2-bridge.service
systemctl restart goodix55a2-bridge.service
systemctl try-restart fprintd.service || true
echo "Installed. Enroll with: fprintd-enroll"
