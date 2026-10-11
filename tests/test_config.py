"""Checks the per-sensor config patching against a real Windows capture.

Run: python3 -m unittest discover -s tests
"""
import importlib.util
import pathlib
import sys
import types
import unittest

# The bridge imports cv2/numpy/usb at module level; the config code needs none of them.
for name in ("cv2", "numpy", "usb", "usb.core", "usb.util"):
    sys.modules.setdefault(name, types.ModuleType(name))

_path = pathlib.Path(__file__).resolve().parent.parent / "bridge" / "goodix55a2-bridge.py"
_spec = importlib.util.spec_from_loader("bridge", loader=None)
bridge = importlib.util.module_from_spec(_spec)
exec(compile(_path.read_text(), str(_path), "exec"), bridge.__dict__)

# OTP and the config Windows uploaded for the sensor in tlambertz/goodix-fingerprint-reversing
# (logs/4_tls_init.log, capture.py). Windows logged "tcode: 0x100, diff: 0x16" for it.
TLAMBERTZ_OTP = bytes.fromhex("0a268766d230f41b68e208d5020402948e00aa690c9cf90601000000c0bfd034")
TLAMBERTZ_CONFIG = bytes.fromhex(
    "301160712c9d2cc91ce518fd00fd00fd03ba000080ca0006008400beb28600c5b98800b5ad8a009d958c0000be8e"
    "0000c5900000b59200009d940000af960000bf980000b69a0000a7d2000000d4000000d6000000d8000000120003"
    "04d0000000700000007200785674003412200010402a0102002200012024003200800001045c0000015600304858"
    "00020032000802660000027c000038820080152a0182032200012024001400800001045c00000156000c24580005"
    "0032000802660000027c000038820080162a0108005c008000540000016200380464001000660000027c0001382a"
    "0108005c0000015200080054000001660000027c00013800e858")

# Second, independent unit (ThinkBook 14s Yoga ITL).
THINKBOOK_OTP = bytes.fromhex("0a072666d210f90f2812d85a0204fc8c8e00a5690994fa0500000000c06bf722")


class ConfigTest(unittest.TestCase):
    def test_calibration_matches_windows_log(self):
        self.assertEqual(bridge.otp_calibration(TLAMBERTZ_OTP), (0x100, 0x16))

    def test_patched_config_matches_windows_capture(self):
        config, _ = bridge.sensor_config(TLAMBERTZ_OTP)
        self.assertEqual(config.hex(), TLAMBERTZ_CONFIG.hex())

    def test_second_unit(self):
        self.assertEqual(bridge.otp_calibration(THINKBOOK_OTP), (0x100, 0x19))
        config, _ = bridge.sensor_config(THINKBOOK_OTP)
        diff = [i for i in range(len(config)) if config[i] != TLAMBERTZ_CONFIG[i]]
        self.assertEqual(diff[:1], [0xC8])            # only the delta byte (+ checksum)
        self.assertEqual(config[0xC8], 0x19)

    def test_invalid_otp_keeps_default(self):
        bad = bytearray(THINKBOOK_OTP)
        bad[0x17] ^= 0xFF
        config, cal = bridge.sensor_config(bytes(bad))
        self.assertIsNone(cal)
        self.assertEqual(config[:-2], bridge.DEFAULT_CONFIG[:-2])


if __name__ == "__main__":
    unittest.main()
