#!/usr/bin/python3
"""Goodix 27c6:55a2 bridge for the goodix55a2 libfprint TOD driver.

Drives the sensor over USB + TLS-PSK, detects touches and matches them with
SIFT + RANSAC against templates in /var/lib/goodix55a2. The TOD driver inside
fprintd sends one request per connection on a root-only UNIX socket:

  ENROLL <stages>      -> FINGER/STAGE/RETRY lines, then DONE <template-id>
  VERIFY <id>          -> MATCH | NOMATCH | RETRY <code>
  IDENTIFY <id> ...    -> MATCH <id> | NOMATCH | RETRY <code>

Only touches that start after the request are used (the finger must be lifted
first), so nothing can be queued or replayed. Closing the connection cancels.

Protocol/config derived from goodix-fp-dump and th0m.as' 55a2 reversing work.
"""
import glob, logging, os, secrets, socket, ssl, struct, sys, time

import cv2

import numpy as np
import usb.core
import usb.util

log = logging.getLogger("goodix55a2")

VID, PID = 0x27C6, 0x55A2
SOCK = os.environ.get("GOODIX_SOCKET", "/run/goodix55a2/bridge.sock")
STATE = os.environ.get("GOODIX_STATE", "/var/lib/goodix55a2")
FPRINT_DB = "/var/lib/fprint"
MATCH_INLIERS = 12        # genuine ~11-60 with a full template, impostor ~0-2
DEBUG_DIR = os.environ.get("GOODIX_DEBUG_DIR")
MIN_KEYPOINTS = 60        # below this a touch is too small/blurry to use
IDLE_CLOSE = 30.0         # release the sensor after this many idle seconds
RETRY_CENTER_FINGER = 2   # FpDeviceRetry codes
RETRY_REMOVE_FINGER = 3

PSK = bytes(32)  # all-zero PSK, provisioned below in white-box form
PSK_WHITE_BOX = bytes.fromhex(
    "ec35ae3abb45ed3f12c4751f1e5c2cc05b3c5452e9104d9f2a3118644f37a04b"
    "6fd66b1d97cf80f1345f76c84f03ff30bb51bf308f2a9875c41e6592cd2a2f9e"
    "60809b17b5316037b69bb2fa5d4c8ac31edb3394046ec06bbdacc57da6a756c5")
PMK_HASH = bytes.fromhex("81b8ff490612022a121a9449ee3aad2792f32b9f3141182cd01019945ee50361")

# Default 55a2 chip config as shipped in the Goodix Windows driver (Wbdi.dll
# 3.1.581.610). Per-sensor values are patched in from the OTP, see sensor_config().
DEFAULT_CONFIG = bytes.fromhex(
    "301160712c9d2cc91ce518fd00fd00fd03ba000080ca0006008400beb28600c5b98800b5ad8a009d958c0000be8e"
    "0000c5900000b59200009d940000af960000bf980000b69a0000a7d2000000d4000000d6000000d8000000120003"
    "04d0000000700000007200785674003412200010402a0102002200012024003200800001045c0000015600304858"
    "00020032000802660000027c000038820080152a0182032200012024001400800001045c00000156000c24580005"
    "0032000802660000027c000038820080152a0108005c008000540000016200380464001000660000027c0001382a"
    "0108005c0080005200080054000001660000027c00013800bad8")
TAG_TCODE, TAG_DELTA_DOWN = 0x5C, 0x82
FDT_MODE = bytes.fromhex("0d0180a08093809b80948090808f8094808b808a8083")

W, H = 176, 56            # output image (columns x rows)
FINGER_ON = 30.0          # ridge-energy thresholds (no finger ~5, finger ~85-120)
FINGER_OFF = 12.0
POLL = 0.08

F_MSG, F_TLS, F_TLS_DATA = 0xA0, 0xB0, 0xB2
C_NOP, C_IMG, C_FDT_MODE, C_CONFIG, C_RESET, C_OTP = 0x00, 0x20, 0x36, 0x90, 0xA2, 0xA6
C_FW, C_ACK, C_DRV, C_TLS_REQ, C_PSK_W, C_PSK_R = 0xA8, 0xB0, 0xC4, 0xD0, 0xE0, 0xE4


def pack(payload, flags=F_MSG):
    hdr = struct.pack("<BH", flags, len(payload))
    return hdr + bytes([sum(hdr) & 0xFF]) + payload


def unpack(data, flags):
    if data[0] != flags or sum(data[:3]) & 0xFF != data[3]:
        raise IOError(f"bad pack {data[:4].hex()}")
    return data[4:4 + struct.unpack("<H", data[1:3])[0]]


def proto(cmd, payload):
    body = struct.pack("<BH", cmd, len(payload) + 1) + payload
    return body + bytes([(0xAA - sum(body)) & 0xFF])


def unproto(data, cmd):
    length = struct.unpack("<H", data[1:3])[0]
    if data[0] != cmd or data[2 + length] != (0xAA - sum(data[:2 + length])) & 0xFF:
        raise IOError(f"bad reply for {cmd:#x}: {data[:8].hex()}")
    return data[3:2 + length]


def otp_calibration(otp):
    """tcode and FDT delta from the OTP, as in Wbdi.dll _MilanFSerGetTcodeAndDiffFromOtp."""
    if len(otp) < 0x18:
        return None
    v = otp[0x16]
    if v == 0 or v + otp[0x17] != 0xFF:
        return None
    tcode = ((v >> 4) + 1) << 4
    diff = ((((v & 0xF) + 2) * 100 << 8) // tcode // 3) >> 4
    return tcode, diff


def config_entry(config, section, tag):
    """Offset of a tag's value in a config section (entries are <tag, value> LE pairs)."""
    base, size = config[1 + section * 2], config[2 + section * 2]
    for e in range(base, base + size, 4):
        if int.from_bytes(config[e:e + 2], "little") == tag:
            return e + 2
    raise ValueError(f"tag {tag:#x} not in section {section}")


def config_fix_checksum(config):
    s = 0xA5A5
    for i in range(0, len(config) - 2, 2):
        s = (s + int.from_bytes(config[i:i + 2], "little")) & 0xFFFF
    config[-2:] = ((0x10000 - s) & 0xFFFF).to_bytes(2, "little")


def sensor_config(otp):
    """Patch the default config with this sensor's calibration, like the Windows driver."""
    config = bytearray(DEFAULT_CONFIG)
    cal = otp_calibration(otp)
    if cal:
        tcode, diff = cal
        e = config_entry(config, 4, TAG_TCODE)
        config[e:e + 2] = tcode.to_bytes(2, "little")
        e = config_entry(config, 2, TAG_DELTA_DOWN)          # delta in the high byte
        config[e + 1] = diff
    config_fix_checksum(config)
    return bytes(config), cal


class Sensor:
    def __init__(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise IOError("sensor not found")
        self.dev = dev
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
        try:
            dev.get_active_configuration()
        except usb.core.USBError:
            dev.set_configuration()
        self._drain()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("PSK")
        ctx.set_psk_server_callback(lambda _ident: PSK)
        self.bin, self.bout = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = ctx.wrap_bio(self.bin, self.bout, server_side=True)

    def close(self):
        usb.util.dispose_resources(self.dev)

    def _drain(self):
        try:
            while True:
                self.dev.read(0x82, 0x10000, 50)
        except usb.core.USBTimeoutError:
            pass

    def write(self, data):
        if len(data) % 64:
            data += bytes(64 - len(data) % 64)
        for i in range(0, len(data), 64):
            self.dev.write(0x01, data[i:i + 64], 2000)

    def read(self, timeout=3000):
        return self.dev.read(0x82, 0x10000, timeout).tobytes()

    def cmd(self, cmd, payload, reply=True, flags=F_MSG):
        self.write(pack(proto(cmd, payload)))
        ack = unproto(unpack(self.read(), F_MSG), C_ACK)
        if ack[0] != cmd:
            raise IOError(f"ack mismatch {ack.hex()} for {cmd:#x}")
        if not reply:
            return None
        data = unpack(self.read(), flags)
        return data if flags != F_MSG else unproto(data, cmd)

    def psk_ok(self):
        r = self.cmd(C_PSK_R, struct.pack("<II", 0xBB020007, 0))
        return r[0] == 0 and r[9:9 + struct.unpack("<I", r[5:9])[0]] == PMK_HASH

    def open(self):
        self.write(pack(proto(C_NOP, bytes(4))[:-1] + b"\x88"))  # nop, no reply expected
        self._drain()
        fw = self.cmd(C_FW, b"\x00\x00").split(b"\x00")[0].decode()
        log.info("firmware %s", fw)
        if not fw.startswith("GF32") or "_RTSEC_APP_" not in fw:
            raise IOError(f"unexpected firmware {fw}")
        if not self.psk_ok():
            # Windows (or a factory reset) re-paired the sensor: re-provision our PSK.
            log.warning("sensor PSK differs, re-provisioning known PSK")
            r = self.cmd(C_PSK_W, struct.pack("<II", 0xBB010003, len(PSK_WHITE_BOX)) + PSK_WHITE_BOX)
            if r[0] != 0 or not self.psk_ok():
                raise IOError("PSK provisioning failed")
        r = self.cmd(C_RESET, bytes([0x05, 20]))
        if r[0] != 1:
            raise IOError("reset failed")
        config, cal = sensor_config(self.cmd(C_OTP, b"\x00\x00"))
        if cal:
            log.info("OTP calibration: tcode %#x, fdt delta %#x", *cal)
        else:
            log.warning("OTP calibration missing or invalid, using default config")
        self._handshake()
        if self.cmd(C_CONFIG, config)[0] != 1:
            raise IOError("config upload failed")
        self.cmd(C_DRV, b"\x01\x00", reply=False)
        self.cmd(C_FDT_MODE, FDT_MODE)

    def _hs_step(self):
        try:
            self.tls.do_handshake()
        except ssl.SSLWantReadError:
            pass

    def _handshake(self):
        self.bin.write(self.cmd(C_TLS_REQ, b"\x00\x00", flags=F_TLS))
        self._hs_step()
        self.write(pack(self.bout.read(), F_TLS))
        for _ in range(3):
            self.bin.write(unpack(self.read(), F_TLS))
        self._hs_step()
        self.write(pack(self.bout.read(), F_TLS))
        time.sleep(0.01)
        log.info("TLS established: %s", self.tls.cipher()[0])

    def image(self):
        rec = self.cmd(C_IMG, b"\x01\x00", flags=F_TLS_DATA)[9:]
        self.bin.write(rec)
        plain = b""
        while True:
            try:
                plain += self.tls.read(65536)
            except ssl.SSLWantReadError:
                break
        raw = np.frombuffer(plain[:-4], np.uint8).reshape(-1, 6).astype(np.uint16)
        px = np.empty((raw.shape[0], 4), np.uint16)
        px[:, 0] = ((raw[:, 0] & 0xF) << 8) | raw[:, 1]
        px[:, 1] = (raw[:, 3] << 4) | (raw[:, 0] >> 4)
        px[:, 2] = ((raw[:, 5] & 0xF) << 8) | raw[:, 2]
        px[:, 3] = (raw[:, 4] << 4) | (raw[:, 5] >> 4)
        return np.flipud(px.reshape(W, H).T).astype(np.float32)  # H rows x W cols


def ridge_energy(diff):
    k = np.ones(5) / 5
    blur = np.apply_along_axis(lambda r: np.convolve(r, k, "same"), 1, diff)
    blur = np.apply_along_axis(lambda c: np.convolve(c, k, "same"), 0, blur)
    return float((diff - blur)[3:-3, 3:-3].std())


def to_u8(diff):
    lo, hi = np.percentile(diff, 1), np.percentile(diff, 99)
    out = np.clip((diff - lo) / max(hi - lo, 1e-6), 0, 1)
    return (255 - out * 255).astype(np.uint8)  # ridges dark on light background


class Matcher:
    def __init__(self, state_dir):
        self.dir = os.path.join(state_dir, "templates")
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        self.sift = cv2.SIFT_create()
        self.clahe = cv2.createCLAHE(2.0, (8, 8))
        self.bf = cv2.BFMatcher()
        self.templates = {}       # id -> list of (pts, desc)
        for path in glob.glob(os.path.join(self.dir, "*.npz")):
            self.templates[os.path.basename(path)[:-4]] = self._load(path)
        log.info("loaded %d templates", len(self.templates))

    def _load(self, path):
        z = np.load(path)
        n = len([k for k in z.files if k.startswith("p")])
        return [(z[f"p{i}"], z[f"d{i}"]) for i in range(n)]

    def save(self, tid, feats):
        arrs = {}
        for i, (p, d) in enumerate(feats):
            arrs[f"p{i}"], arrs[f"d{i}"] = p, d
        tmp = os.path.join(self.dir, f".{tid}.tmp.npz")
        np.savez(tmp, **arrs)
        os.replace(tmp, os.path.join(self.dir, f"{tid}.npz"))
        self.templates[tid] = feats

    def features(self, u8):
        img = self.clahe.apply(cv2.resize(u8, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
        kp, desc = self.sift.detectAndCompute(img, None)
        pts = np.float32([k.pt for k in kp]) if kp else np.zeros((0, 2), np.float32)
        return pts, desc if desc is not None else np.zeros((0, 128), np.float32)

    def inliers(self, a, b):
        (pa, da), (pb, db) = a, b
        if len(da) < 3 or len(db) < 3:
            return 0
        good = [m for m, n in self.bf.knnMatch(da, db, k=2) if m.distance < 0.75 * n.distance]
        if len(good) < 4:
            return 0
        src = pa[[m.queryIdx for m in good]]
        dst = pb[[m.trainIdx for m in good]]
        M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=6.0)
        if M is None:
            return 0
        scale = float(np.hypot(M[0, 0], M[1, 0]))
        rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
        return int(inl.sum()) if 0.85 < scale < 1.18 and abs(rot) < 35 else 0

    def score(self, feat, tid):
        return max((self.inliers(feat, t) for t in self.templates.get(tid, [])), default=0)

    def prune(self, min_age=600):
        """Drop templates that no fprintd print refers to any more."""
        blobs = b""
        for path in glob.glob(os.path.join(FPRINT_DB, "**", "*"), recursive=True):
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    blobs += f.read()
        for tid in list(self.templates):
            path = os.path.join(self.dir, f"{tid}.npz")
            if tid.encode() not in blobs and time.time() - os.path.getmtime(path) > min_age:
                os.unlink(path)
                del self.templates[tid]
                log.info("pruned unused template %s", tid)


class Cancelled(Exception):
    pass


class Bridge:
    def __init__(self):
        self.matcher = Matcher(STATE)
        self.calib_path = os.path.join(STATE, "calib.npy")
        self.sensor = None
        self.calib = None
        self.last_used = 0.0

    # -- sensor session ---------------------------------------------------
    def ensure_sensor(self):
        if self.sensor:
            return
        self.sensor = Sensor()
        try:
            self.sensor.open()
        except Exception:
            self.release()
            raise
        frame = self.sensor.image()
        stored = np.load(self.calib_path) if os.path.exists(self.calib_path) else None
        if stored is not None and stored.shape == frame.shape:
            self.calib = stored
            if ridge_energy(frame - stored) < FINGER_OFF:
                self.calib = frame
        else:
            self.calib = frame               # first run: sensor must be untouched
        np.save(self.calib_path, self.calib)

    def release(self):
        if self.sensor:
            try:
                self.sensor.close()
            except Exception:
                pass
        self.sensor = None

    def frame(self):
        img = self.sensor.image()
        return img, ridge_energy(img - self.calib)

    def wait_touch(self, conn):
        """Wait for a finger that is placed after this call; return its 8-bit image."""
        lifted = False
        while True:
            if peer_closed(conn):
                raise Cancelled()
            img, e = self.frame()
            if e < FINGER_OFF:
                if not lifted:
                    lifted = True
                    reply(conn, "FINGER 0")
                if e < FINGER_OFF / 2:
                    self.calib = 0.95 * self.calib + 0.05 * img   # track slow drift
            elif lifted and e > FINGER_ON:
                time.sleep(0.05)                                   # let the finger settle
                img, e = self.frame()
                if e > FINGER_ON:
                    reply(conn, "FINGER 1")
                    u8 = to_u8(img - self.calib)
                    if DEBUG_DIR:
                        np.save(os.path.join(DEBUG_DIR, f"{time.time():.2f}.npy"), u8)
                    return u8
            time.sleep(POLL)

    def touch_features(self, conn):
        """Return features of a usable touch, or None after asking for a retry."""
        feat = self.matcher.features(self.wait_touch(conn))
        if len(feat[0]) < MIN_KEYPOINTS:
            log.info("touch too small (%d keypoints)", len(feat[0]))
            return None
        return feat

    # -- requests ---------------------------------------------------------
    def enroll(self, conn, stages):
        feats = []
        while len(feats) < stages:
            f = self.touch_features(conn)
            if f is None:
                reply(conn, f"RETRY {RETRY_CENTER_FINGER}")
                continue
            feats.append(f)
            reply(conn, f"STAGE {len(feats)}")
        tid = secrets.token_hex(8)
        self.matcher.save(tid, feats)
        log.info("enrolled template %s", tid)
        reply(conn, f"DONE {tid}")

    def match(self, conn, ids, identify):
        f = self.touch_features(conn)
        if f is None:
            reply(conn, f"RETRY {RETRY_CENTER_FINGER}")
            return
        scores = {tid: self.matcher.score(f, tid) for tid in ids}
        best = max(scores, key=scores.get, default=None)
        log.info("%s scores %s", "identify" if identify else "verify", scores)
        if best is not None and scores[best] >= MATCH_INLIERS:
            reply(conn, f"MATCH {best}" if identify else "MATCH")
        else:
            reply(conn, "NOMATCH")

    def handle(self, conn):
        line = b""
        conn.settimeout(2.0)
        while not line.endswith(b"\n") and len(line) < 4096:
            chunk = conn.recv(4096)
            if not chunk:
                return
            line += chunk
        conn.settimeout(None)
        words = line.decode(errors="replace").split()
        if not words:
            return
        log.info("request %s", words[0])
        self.ensure_sensor()
        if words[0] == "ENROLL":
            self.enroll(conn, max(1, min(int(words[1]), 30)) if len(words) > 1 else 10)
        elif words[0] == "VERIFY" and len(words) == 2:
            self.match(conn, words[1:], identify=False)
        elif words[0] == "IDENTIFY":
            self.match(conn, words[1:], identify=True)
        else:
            reply(conn, "ERROR unknown request")
        np.save(self.calib_path, self.calib)

    def run(self):
        os.makedirs(os.path.dirname(SOCK), mode=0o700, exist_ok=True)
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        srv.bind(SOCK)
        os.umask(old)
        srv.listen(4)
        srv.settimeout(5.0)
        self.matcher.prune()
        log.info("listening on %s", SOCK)
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if self.sensor and time.monotonic() - self.last_used > IDLE_CLOSE:
                    self.release()
                    self.matcher.prune()
                continue
            with conn:
                try:
                    self.handle(conn)
                except Cancelled:
                    log.info("request cancelled")
                except (OSError, IOError, ValueError, usb.core.USBError) as e:
                    log.warning("request failed: %s", e)
                    self.release()
                    try:
                        reply(conn, f"ERROR {e}")
                    except OSError:
                        pass
                self.last_used = time.monotonic()


def reply(conn, msg):
    conn.sendall(msg.encode() + b"\n")


def peer_closed(conn):
    try:
        return conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    Bridge().run()


if __name__ == "__main__":
    main()
