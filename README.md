# Goodix 27c6:55a2 fingerprint reader on Linux

Makes the Goodix `27c6:55a2` USB fingerprint reader work with fprintd (login,
sudo, lock screen) through a small libfprint TOD driver and a Python bridge.

Tested on a Lenovo ThinkBook 14s Yoga ITL (20WE), Fedora 44, firmware
`GF3206_RTSEC_APP_10063` / IAP `MILAN_RTSEC_IAP_10027`. Other laptops with the
same reader should behave the same, but are untested.

> **Read the warnings below before installing.** This is a community driver
> built by reverse engineering. It is not affiliated with Goodix or Lenovo.

## How it works

```
fprintd ─ libfprint-tod ─ goodix55a2 TOD module ──(UNIX socket, root only)──► goodix55a2-bridge ──USB/TLS──► sensor
```

- **`bridge/goodix55a2-bridge.py`** (root service) talks to the sensor: USB
  framing, TLS-PSK session, chip config, image capture (56×176, 12-bit) and
  touch detection. It also does the matching: SIFT features + RANSAC with a
  similarity transform, against templates stored in `/var/lib/goodix55a2`.
  libfprint's own NBIS matcher can't match images this small.
- **`tod/goodix55a2.c`** is a libfprint TOD driver. For every enroll,
  verify or identify it opens a new connection to the bridge and sends one
  request. fprintd stores only a template ID in the print.
- The bridge only uses touches that start **after** the request (the finger has
  to be lifted first), so a touch can never be queued up and replayed later.
  Closing the connection cancels the request.

Bridge protocol (one request per connection, line based):

| Request | Replies |
|---|---|
| `ENROLL <stages>` | `FINGER 0/1`, `STAGE <n>`, `RETRY <code>`, then `DONE <template-id>` |
| `VERIFY <id>` | `MATCH`, `NOMATCH` or `RETRY <code>` |
| `IDENTIFY <id> ...` | `MATCH <id>`, `NOMATCH` or `RETRY <code>` |

## Warnings

- **The sensor's pairing key is overwritten.** The reader talks TLS with a
  pre-shared key (PSK) that Windows or the factory provisioned and that
  can't be read back. On first use the bridge writes the community all-zero
  PSK (white-box form) into the sensor. The firmware is **not** flashed or
  erased.
- **Dual boot with Windows:** Windows Hello fingerprint stops working after
  this. If Windows re-pairs the sensor, the bridge rewrites the key the next
  time it runs, so the two systems will keep taking the pairing from each
  other.
- **Security model:** anything that can write to `/run/goodix55a2/bridge.sock`
  can answer fprintd's match requests, so the directory is root-only (0700).
  The matcher is a heuristic. In testing, other fingers scored 0–3 inliers and
  the enrolled finger 15–90, with the match threshold at 12. Treat it as a
  convenience feature, not a high-assurance biometric.
- No warranty: see [LICENSE](LICENSE).

## Requirements

- libfprint built with TOD support (`libfprint-tod`) and fprintd. On Fedora,
  use the [grahamwhiteuk/libfprint-tod](https://copr.fedorainfracloud.org/coprs/grahamwhiteuk/libfprint-tod/)
  COPR. Ubuntu ships `libfprint-2-tod1`.
- Build: `gcc`, `pkg-config`, glib/gio headers, `libfprint-tod-devel`,
  `libgusb-devel`, `json-glib-devel`.
- Runtime: `python3`, `python3-pyusb`, `python3-numpy`, `python3-opencv`.

Fedora example:

```sh
sudo dnf copr enable grahamwhiteuk/libfprint-tod
sudo dnf install libfprint-tod fprintd fprintd-pam \
    gcc pkgconf glib2-devel libfprint-tod-devel libgusb-devel json-glib-devel \
    python3-pyusb python3-numpy python3-opencv
```

## Install

```sh
git clone https://github.com/<you>/goodix-55a2-linux
cd goodix-55a2-linux
./tod/build.sh
sudo ./install.sh
```

Don't touch the sensor during the first enrollment's opening seconds: the
bridge takes its calibration frame then.

```sh
fprintd-enroll          # 10 touches; lift fully, move slightly between touches
fprintd-verify
```

To use the fingerprint for login and sudo on Fedora:
`sudo authselect enable-feature with-fingerprint`.

Tips: cover the whole finger pad during enrollment (center, edges, a little
rotation). If verification misses often, delete the print and enroll again.

## Uninstall

```sh
sudo ./uninstall.sh
```

This removes the driver, the service, the stored templates and the fprintd
prints made with this driver. The PSK in the sensor stays as it is.

## Troubleshooting

```sh
journalctl -u goodix55a2-bridge -f      # bridge log: requests, scores, errors
fprintd-list $USER                       # should list "Goodix 55a2 Fingerprint Sensor (bridge)"
```

- `goodix55a2-bridge unavailable`: the service isn't running. Check
  `systemctl status goodix55a2-bridge`.
- `unexpected firmware`: your reader runs firmware this project hasn't
  seen. Please open an issue with the firmware string from the log.
- After a libfprint-tod update, rebuild and reinstall the module
  (`./tod/build.sh && sudo ./install.sh`).

## Credits

- [goodix-fp-linux-dev/goodix-fp-dump](https://github.com/goodix-fp-linux-dev/goodix-fp-dump):
  protocol, TLS flow and PSK handling.
- [tlambertz/goodix-fingerprint-reversing](https://github.com/tlambertz/goodix-fingerprint-reversing)
  ([blog post](https://blog.th0m.as/misc/fingerprint-reversing/)): 55a2 chip
  config, FDT format and image geometry.
- [libfprint](https://gitlab.freedesktop.org/libfprint/libfprint) and
  [libfprint-tod](https://gitlab.freedesktop.org/3v1n0/libfprint/-/tree/tod).
