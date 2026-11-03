"""Touch detection: press-to-unlock, baseline safety and recovery from a lost sensor.

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
        bridge.last_capture = None
        bridge.calib_suspect = False
        bridge.reopened = False
        self.bridge = bridge
        self.conn, self.peer = socket.socketpair()

    def tearDown(self):
        self.conn.close()
        self.peer.close()
        self.tmp.cleanup()

    def run_touch(self, frames, accept_present):
        self.bridge.sensor = FakeSensor(frames, self.clock)
        self.bridge.wait_touch(self.conn, accept_present)
        return self.bridge.sensor.read

    def test_energy_levels_are_realistic(self):
        e = self.b.ridge_energy
        self.assertLess(e(empty() - empty()), self.b.FINGER_OFF)
        self.assertGreater(e(finger() - empty()), self.b.FINGER_ON)
        self.assertLess(e(finger() - finger()), self.b.SAME_PLACEMENT_MAX)
        self.assertGreater(e(finger(2) - finger()), self.b.SAME_PLACEMENT_MAX)

    def test_enroll_requires_lift(self):
        used = self.run_touch([finger(), finger(), empty(), finger(), finger()], accept_present=False)
        self.assertEqual(used, 5)

    def test_match_accepts_finger_already_on_sensor(self):
        used = self.run_touch([finger(), finger()], accept_present=True)
        self.assertEqual(used, 2)                       # first frame + settle frame

    def test_same_resting_finger_is_not_used_twice(self):
        self.run_touch([finger(), finger()], accept_present=True)
        used = self.run_touch([finger(), finger(), empty(), finger(1), finger(1)], accept_present=True)
        self.assertEqual(used, 5)

    def test_new_placement_after_capture_is_accepted_without_seen_lift(self):
        # The lift happened between two requests (sensor not polled): a finger in a
        # different position must count at once. This is the real-world unlock case.
        self.run_touch([finger(), finger()], accept_present=True)
        used = self.run_touch([finger(2), finger(2)], accept_present=True)
        self.assertEqual(used, 2)

    def test_resting_finger_does_not_become_the_baseline(self):
        # Finger left on the sensor after a capture for > RECALIBRATE_AFTER.
        self.run_touch([finger(), finger()], accept_present=True)
        resting = [finger()] * 16                       # 8 s at 0.5 s/frame
        self.run_touch(resting + [empty(), finger(1), finger(1)], accept_present=True)
        self.assertTrue(np.array_equal(self.bridge.calib, empty()))
        self.assertFalse(self.bridge.calib_suspect)

    def test_stale_baseline_still_recovers(self):
        self.bridge.calib = finger()                    # poisoned baseline, no finger present
        frames = [empty()] * 14 + [finger(1), finger(1)]
        self.run_touch(frames, accept_present=False)
        self.assertTrue(np.array_equal(self.bridge.calib, empty()))

    def test_recalibrated_baseline_is_not_saved_until_confirmed(self):
        self.bridge.sensor = FakeSensor([], self.clock)
        self.bridge.calib_suspect = True
        self.bridge.save_calib()
        self.assertFalse(pathlib.Path(self.bridge.calib_path).exists())
        self.bridge.calib_suspect = False
        self.bridge.save_calib()
        self.assertTrue(pathlib.Path(self.bridge.calib_path).exists())

    def fake_matcher(self, score):
        self.bridge.matcher = types.SimpleNamespace(
            features=lambda u8: (np.zeros((100, 2), np.float32), np.zeros((100, 128), np.float32)),
            score=lambda feat, tid: score)

    def test_recalibrated_baseline_confirmed_only_by_match(self):
        self.bridge.calib = finger()                    # poisoned baseline, no finger present
        self.run_touch([empty()] * 14 + [finger(1), finger(1)], accept_present=False)
        self.assertTrue(self.bridge.calib_suspect)      # frames after a re-take prove nothing
        self.fake_matcher(score=3)
        self.bridge.sensor = FakeSensor([empty(), finger(2), finger(2)], self.clock)
        self.bridge.match(self.conn, ["t1"], identify=False)
        self.assertTrue(self.bridge.calib_suspect)      # NOMATCH: still unconfirmed
        self.fake_matcher(score=40)
        self.bridge.sensor = FakeSensor([empty(), finger(3), finger(3)], self.clock)
        self.bridge.match(self.conn, ["t1"], identify=False)
        self.assertFalse(self.bridge.calib_suspect)     # MATCH confirms it
        self.assertIn(b"NOMATCH", self.peer.recv(4096))

    def test_no_accept_present_while_baseline_suspect(self):
        self.bridge.calib_suspect = True
        used = self.run_touch([finger(), finger(), empty(), finger(1), finger(1)], accept_present=True)
        self.assertEqual(used, 5)

    def test_reopens_sensor_lost_mid_request(self):
        reopened = FakeSensor([finger(), finger()], self.clock)

        def ensure():
            self.bridge.sensor = reopened
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("No such device (it may have been disconnected)")], self.clock)
        self.bridge.wait_touch(self.conn, accept_present=True)
        self.assertIs(self.bridge.sensor, reopened)

    def test_second_loss_in_same_request_fails(self):
        def ensure():
            self.bridge.sensor = FakeSensor([USBError("gone again")], self.clock)
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("gone")], self.clock)
        with self.assertRaises(USBError):
            self.bridge.wait_touch(self.conn, accept_present=True)

    def test_reopen_retries_while_usb_reenumerates(self):
        calls = []

        def ensure():
            calls.append(1)
            if len(calls) < 3:
                raise USBError("sensor not found")
            self.bridge.sensor = FakeSensor([finger(), finger()], self.clock)
        self.bridge.ensure_sensor = ensure
        self.bridge.sensor = FakeSensor([USBError("gone")], self.clock)
        self.bridge.wait_touch(self.conn, accept_present=True)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
