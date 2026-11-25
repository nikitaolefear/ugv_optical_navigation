import os
import threading
import time
import unittest

from pymavlink.dialects.v20 import ardupilotmega as mav

from pixhawk_telemetry import PORT, PixhawkTelemetry, PortFailure


class ReconnectTests(unittest.TestCase):
    heartbeat = mav.MAVLink_heartbeat_message(10, 3, 0, 0, 4, 3).pack(
        mav.MAVLink(None, srcSystem=1, srcComponent=1)
    )

    def start_reader(self, opener, **kwargs):
        reader = PixhawkTelemetry(opener=opener, **kwargs)
        reader.reconnect_interval_s = 0.15
        reader.start()
        self.addCleanup(reader.close)
        return reader

    def pipe(self):
        read_fd, write_fd = os.pipe()
        writer = os.fdopen(write_fd, 'wb', buffering=0)
        self.addCleanup(writer.close)
        return read_fd, writer

    def await_snapshot(self, reader, condition):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            sample = reader.snapshot()
            if condition(sample):
                return sample
            time.sleep(0.005)
        self.fail(f'Unexpected telemetry: {reader.snapshot()}')

    def await_disarmed(self, reader):
        return self.await_snapshot(reader, lambda s: s['armed']['value'] == 'DISARMED')

    def test_usb_disconnect_marks_data_stale_and_closes_fd(self):
        fd, writer = self.pipe()
        attempts = []

        def open_checked():
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                return fd
            raise PortFailure('PORT_ERROR', 'tty_stat', PORT, 'USB absent')

        reader = self.start_reader(open_checked)
        writer.write(self.heartbeat)
        self.await_disarmed(reader)
        unplugged = time.monotonic()
        writer.close()
        sample = self.await_snapshot(reader, lambda s: s['connection'] == 'DISCONNECTED')
        self.assertEqual(sample['armed'], {'state': 'STALE', 'value': None})
        self.assertEqual(sample['messages']['HEARTBEAT']['state'], 'STALE')
        self.await_snapshot(reader, lambda s: s['connection'] == 'PORT_ERROR')
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertGreaterEqual(attempts[1] - unplugged, reader.reconnect_interval_s)

    def test_usb_reconnect_requires_new_heartbeat(self):
        first, old_writer = self.pipe()
        second, new_writer = self.pipe()
        present = threading.Event()
        attempts = []

        def open_checked():
            attempts.append(1)
            if len(attempts) == 1:
                return first
            if present.is_set():
                return second
            raise PortFailure('PORT_ERROR', 'tty_stat', PORT, 'USB absent')

        reader = self.start_reader(open_checked)
        old_writer.write(self.heartbeat)
        self.await_disarmed(reader)
        old_writer.close()
        self.await_snapshot(reader, lambda s: s['connection'] == 'PORT_ERROR')
        present.set()
        sample = self.await_snapshot(reader, lambda s: s['connection'] == 'UNKNOWN')
        self.assertIsNone(sample['armed']['value'])
        self.assertIsNone(reader.messages.get('HEARTBEAT'))
        new_writer.write(self.heartbeat)
        self.assertEqual(self.await_disarmed(reader)['connection'], 'CONNECTED')
        self.assertEqual(reader.heartbeat_serial, 2)

    def test_busy_port_waits_and_retries_checked_opener(self):
        fd, writer = self.pipe()
        attempts = []

        def open_checked():
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                raise PortFailure('PORT_BUSY', 'fuser_before', PORT, 'Foreign owner')
            return fd

        reader = self.start_reader(open_checked)
        self.await_snapshot(reader, lambda s: s['connection'] == 'PORT_BUSY')
        writer.write(self.heartbeat)
        self.await_disarmed(reader)
        self.assertEqual(len(attempts), 2)
        self.assertGreaterEqual(attempts[1] - attempts[0], reader.reconnect_interval_s)

    def test_stale_heartbeat_on_open_port_is_not_disarmed(self):
        fd, writer = self.pipe()
        reader = self.start_reader(lambda: fd, timeout=0.1)
        writer.write(self.heartbeat)
        self.await_disarmed(reader)
        sample = self.await_snapshot(reader, lambda s: s['connection'] == 'STALE')
        self.assertEqual(sample['armed'], {'state': 'STALE', 'value': None})
        writer.write(self.heartbeat)
        self.assertEqual(self.await_disarmed(reader)['connection'], 'CONNECTED')


if __name__ == '__main__':
    unittest.main()
