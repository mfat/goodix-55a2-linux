"""Touch detection: press-to-unlock and recovery from a lost sensor.

Run: python3 -m unittest discover -s tests
Needs numpy (skipped otherwise); cv2 and pyusb are stubbed.
"""
import importlib.util
import pathlib
import socket
import sys
import time
import types
import unittest

# test_config may have stubbed numpy; this test needs the real one.
if not hasattr(sys.modules.get("numpy"), "zeros"):
    sys.modules.pop("numpy", None)
try:
    import numpy as np
except ImportError:
    np = None

for name in ("cv2", "usb", "usb.core", "usb.util"):
    sys.modules.setdefault(name, types.ModuleType(name))


class USBError(IOError):
    pass


if not hasattr(sys.modules["usb.core"], "USBError"):
    sys.modules["usb.core"].USBError = USBError
sys.modules["usb"].core = sys.modules["usb.core"]
sys.modules["usb"].util = sys.modules["usb.util"]
USBError = sys.modules["usb.core"].USBError


def load_bridge():
    path = pathlib.Path(__file__).resolve().parent.parent / "bridge" / "goodix55a2-bridge.py"
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("bridge_touch", loader=None))
    exec(compile(path.read_text(), str(path), "exec"), mod.__dict__)
    mod.POLL = 0
    mod.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic, time=time.time)
    return mod


def empty():
    return np.zeros((56, 176), np.float32)


def finger():
    cols = np.arange(176, dtype=np.float32)
    return np.tile(300 * np.sin(cols * 2 * np.pi / 4), (56, 1)).astype(np.float32)  # ridges


class FakeSensor:
    def __init__(self, frames):
        self.frames = list(frames)
        self.read = 0

    def image(self):
        if not self.frames:
            raise AssertionError("ran out of frames: touch was not accepted when expected")
        self.read += 1
        f = self.frames.pop(0)
        if isinstance(f, Exception):
            raise f
        return f

    def close(self):
        pass


@unittest.skipIf(np is None, "numpy not installed")
class TouchTest(unittest.TestCase):
    def setUp(self):
        self.b = load_bridge()
        bridge = self.b.Bridge.__new__(self.b.Bridge)
        bridge.calib = empty()
        bridge.consumed = False
        bridge.reopened = False
        bridge.save_calib = lambda: None
        self.bridge = bridge
        self.conn, self.peer = socket.socketpair()

    def tearDown(self):
        self.conn.close()
        self.peer.close()

    def run_touch(self, frames, accept_present):
        self.bridge.sensor = FakeSensor(frames)
        self.bridge.wait_touch(self.conn, accept_present)
        return self.bridge.sensor.read

    def test_energy_levels_are_realistic(self):
        self.assertLess(self.b.ridge_energy(empty() - empty()), self.b.FINGER_OFF)
        self.assertGreater(self.b.ridge_energy(finger() - empty()), self.b.FINGER_ON)

    def test_enroll_requires_lift(self):
        # finger already down: ignored until lifted and placed again
        used = self.run_touch([finger(), finger(), empty(), finger(), finger()], accept_present=False)
        self.assertEqual(used, 5)

    def test_match_accepts_finger_already_on_sensor(self):
        used = self.run_touch([finger(), finger()], accept_present=True)
        self.assertEqual(used, 2)          # first frame + settle frame
        self.assertTrue(self.bridge.consumed)

    def test_same_placement_is_not_used_twice(self):
        self.run_touch([finger(), finger()], accept_present=True)
        # finger still resting: next request must wait for a lift
        used = self.run_touch([finger(), finger(), empty(), finger(), finger()], accept_present=True)
        self.assertEqual(used, 5)

    def test_lift_seen_in_cancelled_request_clears_consumed(self):
        # A: finger captured. B: finger lifted, then the request is cancelled.
        # C: finger pressed again before the request -> must be accepted at once.
        self.run_touch([finger(), finger()], accept_present=True)
        self.assertTrue(self.bridge.consumed)
        frames = [empty(), empty()]

        def cancel_after_lift():
            if not frames:
                self.peer.close()
                return empty()
            return frames.pop(0)
        self.bridge.sensor = types.SimpleNamespace(image=cancel_after_lift, close=lambda: None)
        with self.assertRaises(self.b.Cancelled):
            self.bridge.wait_touch(self.conn, accept_present=True)
        self.assertFalse(self.bridge.consumed)
        self.conn.close()
        self.conn, self.peer = socket.socketpair()
        used = self.run_touch([finger(), finger()], accept_present=True)
        self.assertEqual(used, 2)

    def test_release_clears_consumed(self):
        self.bridge.consumed = True
        self.bridge.sensor = None
        self.bridge.release()
        self.assertFalse(self.bridge.consumed)

    def test_reopens_sensor_lost_mid_request(self):
        reopened = FakeSensor([finger(), finger()])

        def ensure():
            self.bridge.sensor = reopened
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("No such device (it may have been disconnected)")])
        self.bridge.wait_touch(self.conn, accept_present=True)
        self.assertIs(self.bridge.sensor, reopened)
        self.assertTrue(self.bridge.reopened)

    def test_second_loss_in_same_request_fails(self):
        def ensure():
            self.bridge.sensor = FakeSensor([USBError("gone again")])
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("gone")])
        with self.assertRaises(USBError):
            self.bridge.wait_touch(self.conn, accept_present=True)

    def test_reopen_retries_while_usb_reenumerates(self):
        calls = []

        def ensure():
            calls.append(1)
            if len(calls) < 3:
                raise USBError("sensor not found")
            self.bridge.sensor = FakeSensor([finger(), finger()])
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("gone")])
        self.bridge.wait_touch(self.conn, accept_present=True)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
