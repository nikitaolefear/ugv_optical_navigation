"""Bounded binary usbmon capture. Opens only the selected usbmon character device."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import stat
import struct
import time

HEADER = struct.Struct("<QBBBBHBBqiiII")
CAP = 65536
STATS = (2 << 30) | (8 << 16) | (0x92 << 8) | 3


class GetArg(ctypes.Structure):
    _fields_ = [("header", ctypes.c_void_p), ("data", ctypes.c_void_p),
                ("size", ctypes.c_size_t)]


GETX = (1 << 30) | (ctypes.sizeof(GetArg) << 16) | (0x92 << 8) | 10


def mapping(tty):
    root = Path("/sys/class/tty") / tty
    control = (root / "device").resolve(strict=True)
    if not (control / "bInterfaceNumber").is_file():
        control = control.parent
    device = control.parent

    def read(path, name, radix=10):
        return int((path / name).read_text(), radix)

    # Parse the CDC union descriptor and select the companion interface.
    descriptors = (device / "descriptors").read_bytes()
    control_num = read(control, "bInterfaceNumber", 16)
    union = set()
    position = 0
    while position < len(descriptors):
        record = descriptors[position:]
        length = record[0]
        if length < 2 or length > len(record):
            raise ValueError("malformed USB descriptor")
        if length == 5 and record[1:4] == bytes([0x24, 6, control_num]):
            union.add(record[4])
        position += length
    if len(union) != 1:
        raise ValueError("ambiguous CDC union")
    data_num = union.pop()
    companions = [p for p in device.glob(device.name + ":*")
                  if read(p, "bInterfaceNumber", 16) == data_num
                  and read(p, "bInterfaceClass", 16) == 10]
    if len(companions) != 1:
        raise ValueError("ambiguous CDC data interface")
    endpoints = [read(p, "bEndpointAddress", 16) for p in companions[0].glob("ep_*")
                 if (p / "type").read_text().strip() == "Bulk"
                 and (p / "direction").read_text().strip() == "out"]
    if len(endpoints) != 1:
        raise ValueError("ambiguous bulk OUT endpoint")
    return dict(bus=read(device, "busnum"), device=read(device, "devnum"),
                endpoint=endpoints[0], sysfs=str(device), tty=tty,
                inode=device.stat().st_ino)


def clock_pair():
    start = time.monotonic_ns()
    wall = time.time_ns()
    return [start, wall, time.monotonic_ns()]


def read_event(fd, head, data):
    arg = GetArg(ctypes.addressof(head), ctypes.addressof(data), len(data))
    fcntl.ioctl(fd, GETX, bytes(arg))
    urb, event, kind, ep, dev, bus, _, no_data, sec, usec, status, length, captured = HEADER.unpack_from(head)
    return dict(urb=urb, event=chr(event), kind=kind, ep=ep, dev=dev, bus=bus,
                wall_ns=sec*1_000_000_000+usec*1000, status=status,
                length=length, captured=captured, no_data=no_data,
                payload=data.raw[:min(captured,len(data))].hex() if not no_data else "")


class Archive:
    def __init__(self, stream, quota):
        self.stream, self.quota, self.used = stream, quota, 0

    def write(self, row, final=False):
        data = (json.dumps(row, separators=(",", ":")) + "\n").encode()
        if self.used + len(data) > self.quota - (0 if final else 1024):
            raise OverflowError("CAPTURE_QUOTA")
        self.stream.write(data)
        self.used += len(data)


def capture(tty, output, seconds, quota):
    if not 1 <= seconds <= 60 or not 8192 <= quota <= 64*1024*1024:
        raise ValueError("limits")
    key = mapping(tty)
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    fd = os.open(f"/dev/usbmon{key['bus']}", flags)
    stop = False

    def cancel(*_):
        nonlocal stop
        stop = True

    saved = {}
    try:
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            raise ValueError("usbmon device required")
        for name in (signal.SIGINT, signal.SIGTERM):
            saved[name] = signal.signal(name, cancel)
        head, data = ctypes.create_string_buffer(64), ctypes.create_string_buffer(CAP)
        stats = bytearray(8)
        buffered = 0
        dropped = 0
        poll = select.poll()
        poll.register(fd, select.POLLIN)
        next_check = 0.
        end = time.monotonic()+seconds
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600), "wb") as stream:
            write = Archive(stream, quota).write
            write(dict(start=clock_pair(), mapping=key))
            failure = None
            try:
                while not stop and time.monotonic() < end:
                    now = time.monotonic()
                    if now >= next_check:
                        if mapping(tty) != key:
                            raise ValueError("USB_MAPPING_CHANGED")
                        fcntl.ioctl(fd, STATS, stats, True)
                        buffered, losses = struct.unpack("=II", stats)
                        dropped += losses
                        if losses:
                            raise ValueError("CAPTURE_DROPS")
                        next_check = now + .25
                    ready = poll.poll(50)
                    if any(events & (select.POLLERR | select.POLLHUP | select.POLLNVAL)
                           for _, events in ready):
                        raise OSError("USBMON_POLL_ERROR")
                    if not ready:
                        continue
                    try:
                        row = read_event(fd, head, data)
                    except BlockingIOError:
                        continue
                    if ((row["bus"], row["dev"], row["ep"], row["kind"]) ==
                            (key["bus"],key["device"],key["endpoint"],3)):
                        write(row)
            except (OSError, ValueError, OverflowError) as error:
                failure = str(error)
            try:
                fcntl.ioctl(fd, STATS, stats, True)
                buffered, losses = struct.unpack("=II", stats)
                dropped += losses
                if mapping(tty) != key:
                    failure = failure or "USB_MAPPING_CHANGED"
            except (OSError, ValueError) as error:
                failure = failure or str(error)
            write(dict(end=clock_pair(), dropped=dropped, undrained=buffered,
                       interrupted=stop, failure=failure), final=True)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
        for name, handler in saved.items():
            signal.signal(name,handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tty", choices=("ttyACM0","ttyACM1"), required=True)
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--max-bytes", type=int, default=8*1024*1024)
    parser.add_argument("--output", required=True)
    a = parser.parse_args()
    capture(a.tty,a.output,a.seconds,a.max_bytes)
