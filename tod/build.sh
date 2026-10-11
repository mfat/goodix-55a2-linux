#!/bin/bash
# Builds the libfprint TOD module.
# Needs gcc and the headers from libfprint-tod-devel, libgusb-devel and json-glib-devel.
# TOD_INC can point at an alternative include root (e.g. extracted RPMs).
set -euo pipefail
cd "$(dirname "$0")"
T=${TOD_INC:-/usr/include}
TOD_LIB=
for f in /usr/lib64/libfprint-2-tod.so.1 /usr/lib/x86_64-linux-gnu/libfprint-2-tod.so.1 /usr/lib/libfprint-2-tod.so.1; do
  [ -e "$f" ] && { TOD_LIB=$f; break; }
done
[ -n "$TOD_LIB" ] || { echo "libfprint-2-tod.so.1 not found: install libfprint-tod" >&2; exit 1; }

gcc -shared -fPIC -O2 -Wall -Wno-unused-parameter -o libfprint-tod-goodix55a2.so goodix55a2.c \
  -I"$T/libfprint-2/tod-1" -I"$T/libfprint-2" -I"$T/gusb-1" -I"$T/json-glib-1.0" -I/usr/include/json-glib-1.0 \
  $(pkg-config --cflags gio-unix-2.0) "$TOD_LIB" $(pkg-config --libs gio-unix-2.0)
echo "built tod/libfprint-tod-goodix55a2.so"
