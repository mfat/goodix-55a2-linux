#!/bin/sh
# Load/unload the goodix55a2 SELinux module and its file context. Run as root.
#   goodix55a2-selinux install <path-to-goodix55a2.pp>
#   goodix55a2-selinux remove
set -e
command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled || exit 0
case "$1" in
  install)
    semodule -i "$2"
    semanage fcontext -a -t goodix55a2_run_t '/run/goodix55a2(/.*)?' 2>/dev/null \
      || semanage fcontext -m -t goodix55a2_run_t '/run/goodix55a2(/.*)?'
    [ -e /run/goodix55a2 ] && restorecon -R /run/goodix55a2 || true
    ;;
  remove)
    semanage fcontext -d '/run/goodix55a2(/.*)?' 2>/dev/null || true
    semodule -r goodix55a2 2>/dev/null || true
    ;;
esac
