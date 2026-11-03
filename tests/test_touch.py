"""Touch detection: baseline safety and recovery from a lost sensor.

Run: python3 -m unittest discover -s tests
Needs numpy (skipped otherwise); cv2 and pyusb are stubbed.
"""
import importlib.util
import pathlib
import socket
import sys
import tempfile
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
if not hasattr(sys.modules["usb.core"], "USBError"):
    sys.modules["usb.core"].USBError = type("USBError", (IOError,), {})
sys.modules["usb"].core = sys.modules["usb.core"]
sys.modules["usb"].util = sys.modules["usb.util"]
USBError = sys.modules["usb.core"].USBError


class Clock:
    """Fake time: every sensor frame takes FRAME seconds."""
    FRAME = 0.5

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        pass


def load_bridge(clock):
    path = pathlib.Path(__file__).resolve().parent.parent / "bridge" / "goodix55a2-bridge.py"
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("bridge_touch", loader=None))
    exec(compile(path.read_text(), str(path), "exec"), mod.__dict__)
    mod.POLL = 0
    mod.time = types.SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic, time=lambda: clock.t)
    return mod


def empty():
    return np.zeros((56, 176), np.float32)


def finger(shift=0):
    cols = np.arange(176, dtype=np.float32) + shift
    return np.tile(300 * np.sin(cols * 2 * np.pi / 4), (56, 1)).astype(np.float32)  # ridges


class FakeSensor:
    def __init__(self, frames, clock):
        self.frames = list(frames)
        self.clock = clock
        self.read = 0
        self.config = b"cfg"

    def image(self):
        if not self.frames:
            raise AssertionError("ran out of frames: touch was not accepted when expected")
        self.read += 1
        self.clock.t += Clock.FRAME
        f = self.frames.pop(0)
        if isinstance(f, Exception):
            raise f
        return f

    def close(self):
        pass


@unittest.skipIf(np is None, "numpy not installed")
class TouchTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.b = load_bridge(self.clock)
        self.tmp = tempfile.TemporaryDirectory()
        bridge = self.b.Bridge.__new__(self.b.Bridge)
        bridge.calib = empty()
        bridge.calib_path = pathlib.Path(self.tmp.name, "calib.npz").as_posix()
        bridge.calib_confirmed = True
        bridge.last_capture = None
        bridge.reopened = False
        bridge.sensor = None
        self.bridge = bridge
        self.conn, self.peer = socket.socketpair()

    def tearDown(self):
        self.conn.close()
        self.peer.close()
        self.tmp.cleanup()

    def run_touch(self, frames):
        self.bridge.sensor = FakeSensor(frames, self.clock)
        self.bridge.wait_touch(self.conn)
        return self.bridge.sensor.read

    def fake_matcher(self, score):
        self.bridge.matcher = types.SimpleNamespace(
            features=lambda u8: (np.zeros((100, 2), np.float32), np.zeros((100, 128), np.float32)),
            score=lambda feat, tid: score)

    def open_with(self, frames):
        """ensure_sensor() with a fake sensor producing frames after open."""
        self.b.Sensor = lambda: FakeSensor(frames, self.clock)
        FakeSensor.open = lambda self_: None
        self.bridge.ensure_sensor()

    # -- energy model --------------------------------------------------------
    def test_energy_levels_are_realistic(self):
        e = self.b.ridge_energy
        self.assertLess(e(empty() - empty()), self.b.FINGER_OFF)
        self.assertGreater(e(finger() - empty()), self.b.FINGER_ON)
        self.assertLess(e(finger() - finger()), self.b.SAME_PLACEMENT_MAX)
        self.assertGreater(e(finger(2) - finger()), self.b.SAME_PLACEMENT_MAX)

    # -- behaviour kept from upstream ----------------------------------------
    def test_touch_requires_lift_first(self):
        used = self.run_touch([finger(), finger(), empty(), finger(1), finger(1)])
        self.assertEqual(used, 5)

    def test_stale_baseline_still_recovers(self):
        self.bridge.calib = finger()                    # bad baseline, no finger present
        self.run_touch([empty()] * 14 + [finger(1), finger(1)])
        self.assertTrue(np.array_equal(self.bridge.calib, empty()))

    # -- resting finger never becomes the baseline ---------------------------
    def test_resting_finger_does_not_become_the_baseline(self):
        self.run_touch([empty(), finger(), finger()])
        self.run_touch([finger()] * 16 + [empty(), finger(1), finger(1)])   # 8 s resting
        self.assertTrue(np.array_equal(self.bridge.calib, empty()))
        self.assertTrue(self.bridge.calib_confirmed)

    # -- only confirmed baselines are saved -----------------------------------
    def test_unconfirmed_baseline_is_not_saved(self):
        self.bridge.sensor = FakeSensor([], self.clock)
        self.bridge.calib_confirmed = False
        self.bridge.save_calib()
        self.assertFalse(pathlib.Path(self.bridge.calib_path).exists())
        self.bridge.calib_confirmed = True
        self.bridge.save_calib()
        self.assertTrue(pathlib.Path(self.bridge.calib_path).exists())

    def test_recalibration_is_unconfirmed_until_a_match(self):
        self.bridge.calib = finger()
        self.run_touch([empty()] * 14 + [finger(1), finger(1)])
        self.assertFalse(self.bridge.calib_confirmed)
        self.fake_matcher(score=3)
        self.bridge.sensor = FakeSensor([empty(), finger(2), finger(2)], self.clock)
        self.bridge.match(self.conn, ["t1"], identify=False)
        self.assertFalse(self.bridge.calib_confirmed)    # NOMATCH proves nothing
        self.fake_matcher(score=40)
        self.bridge.sensor = FakeSensor([empty(), finger(3), finger(3)], self.clock)
        self.bridge.match(self.conn, ["t1"], identify=False)
        self.assertTrue(self.bridge.calib_confirmed)     # MATCH confirms it

    def test_fresh_baseline_after_open_is_unconfirmed(self):
        self.open_with([empty(), empty()])               # no calib.npz on disk
        self.assertFalse(self.bridge.calib_confirmed)
        self.assertFalse(pathlib.Path(self.bridge.calib_path).exists())

    def test_stored_baseline_is_trusted(self):
        self.bridge.sensor = types.SimpleNamespace(config=b"cfg")
        self.bridge.calib = empty()
        self.bridge.save_calib()
        self.bridge.sensor = None
        self.open_with([empty(), empty()])
        self.assertTrue(self.bridge.calib_confirmed)

    def test_baseline_waits_for_image_to_settle(self):
        warm = [finger(i) for i in range(3)]             # changing frames right after open
        self.open_with(warm + [empty(), empty()])
        self.assertTrue(np.array_equal(self.bridge.calib, empty()))

    # -- suspend/resume ------------------------------------------------------
    def test_reopens_sensor_lost_mid_request(self):
        reopened = FakeSensor([empty(), finger(), finger()], self.clock)

        def ensure():
            self.bridge.sensor = reopened
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("No such device (it may have been disconnected)")], self.clock)
        self.bridge.wait_touch(self.conn)
        self.assertIs(self.bridge.sensor, reopened)

    def test_second_loss_in_same_request_fails(self):
        def ensure():
            self.bridge.sensor = FakeSensor([USBError("gone again")], self.clock)
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("gone")], self.clock)
        with self.assertRaises(USBError):
            self.bridge.wait_touch(self.conn)

    def test_reopen_retries_while_usb_reenumerates(self):
        calls = []

        def ensure():
            calls.append(1)
            if len(calls) < 3:
                raise USBError("sensor not found")
            self.bridge.sensor = FakeSensor([empty(), finger(), finger()], self.clock)
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("gone")], self.clock)
        self.bridge.wait_touch(self.conn)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
