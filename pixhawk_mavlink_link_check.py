"""Receive-only MAVLink link check. No commands, heartbeat sends or settings writes.

Serial: one baud by default; --scan-baud explicitly tries four rates on ONLY
the selected port. Also accepts udpin:HOST:PORT for a passive UDP listener.
Uses pymavlink's parser with no output transport, never a MAVLink transmitter.
"""
import argparse
from collections import Counter
import glob
import importlib.util
import math
import os
import select
import socket
import time

BAUD_RATES = (115200, 230400, 460800, 57600)
PATTERNS = ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*", "/dev/ttyAMA*")


def inventory():
    devices = []
    print("Serial devices:")
    for pattern in PATTERNS:
        paths = sorted(glob.glob(pattern))
        print(f"  {pattern}: " + (", ".join(paths) if paths else "none"))
        devices.extend(paths)
    # Stable USB names first, USB CDC second, UART last. This is not identity proof.
    recommended = devices[0] if devices else None
    print("Recommended candidate: " + (recommended or "none; connect Pixhawk and rerun --list"))
    print("pymavlink installed: " + str(importlib.util.find_spec("pymavlink") is not None))
    return recommended


class Receiver:
    def __init__(self, connection, baud):
        self.serial = None
        self.sock = None
        if connection.startswith("udpin:"):
            host, port = connection[6:].rsplit(":", 1)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                self.sock.bind((host, int(port)))
                self.sock.setblocking(False)
            except Exception:
                self.sock.close()
                raise
        else:
            if not os.path.exists(connection):
                raise ValueError(f"Serial device does not exist: {connection}")
            import serial
            # Set local receive speed only; no autopilot parameter changes.
            # Start closed to keep hardware flow-control lines deasserted on open.
            port = serial.Serial(port=None, baudrate=baud, timeout=0,
                                 xonxoff=False, rtscts=False, dsrdtr=False,
                                 exclusive=True)
            port.dtr = False
            port.rts = False
            port.port = connection
            try:
                port.open()
            except Exception:
                port.close()
                raise
            self.serial = port

    def read(self, timeout):
        endpoint = self.sock if self.sock is not None else self.serial
        ready, _, _ = select.select([endpoint], [], [], max(0., timeout))
        if not ready:
            return b""
        if self.sock is not None:
            return self.sock.recv(65535)
        return self.serial.read(min(4096, max(1, self.serial.in_waiting)))

    def close(self):
        if self.serial is not None:
            self.serial.close()
        if self.sock is not None:
            self.sock.close()


def enum_name(dialect, group, value):
    entry = dialect.enums.get(group, {}).get(value)
    return f"{value} ({entry.name if entry is not None else 'unknown'})"


def listen(connection, baud, dialect):
    receiver = Receiver(connection, baud)
    # None means no write-capable output object exists in this parser.
    parser = dialect.MAVLink(None)
    parser.robust_parsing = True
    deadline = time.monotonic() + 10.
    heartbeat = None
    sample_end = None
    counts = Counter()
    bad_data = 0
    try:
        print(f"Listening on {connection}, baud={baud}; autopilot HEARTBEAT timeout=10 s", flush=True)
        while time.monotonic() < (sample_end if sample_end is not None else deadline):
            end = sample_end if sample_end is not None else deadline
            data = receiver.read(min(.2, max(0., end - time.monotonic())))
            for byte in data:
                message = parser.parse_char(bytes((byte,)))
                if message is None:
                    continue
                kind = message.get_type()
                if kind == "BAD_DATA":
                    bad_data += 1
                    continue
                if heartbeat is None:
                    if (kind != "HEARTBEAT" or
                            message.autopilot == dialect.MAV_AUTOPILOT_INVALID or
                            message.get_srcSystem() == 0):
                        continue
                    heartbeat = message
                    sample_end = time.monotonic() + 5.
                    print(f"system id: {message.get_srcSystem()}")
                    print(f"component id: {message.get_srcComponent()}")
                    print("vehicle type: " + enum_name(dialect, "MAV_TYPE", message.type))
                    print("autopilot type: " + enum_name(dialect, "MAV_AUTOPILOT", message.autopilot))
                    raw = bytes(message.get_msgbuf())
                    wire = "2.0" if raw and raw[0] == 0xFD else "1.0"
                    print(f"MAVLink wire version: {wire}; HEARTBEAT mavlink_version field: {message.mavlink_version}")
                    print(f"base_mode: {message.base_mode}")
                    print(f"custom_mode: {message.custom_mode}")
                    print("Counting received message types for about 5 seconds...")
                    continue
                counts[kind] += 1
        if heartbeat is None:
            print(f"No autopilot HEARTBEAT within 10 s; BAD_DATA blocks={bad_data}")
            return False
        print("Most frequent received message types (all sources on selected link):")
        for kind, count in counts.most_common(15):
            print(f"  {kind}: {count}")
        if not counts:
            print("  none after initial heartbeat")
        print(f"BAD_DATA blocks: {bad_data}")
        return True
    finally:
        receiver.close()


def main():
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("connection", nargs="?", help="serial device or udpin:HOST:PORT")
    argument_parser.add_argument("--list", action="store_true", help="inventory only, do not open a port")
    rates = argument_parser.add_mutually_exclusive_group()
    rates.add_argument("--baud", type=int, choices=BAUD_RATES, default=None)
    rates.add_argument("--scan-baud", action="store_true",
                       help="try 115200,230400,460800,57600 once each on selected serial port")
    args = argument_parser.parse_args()
    inventory()
    print("Typical serial baud rates: " + ", ".join(map(str, BAUD_RATES)))
    if args.list:
        return 0
    if not args.connection:
        print("PIXHAWK MAVLINK LINK: FAIL — no connection specified; use a discovered device.")
        return 1
    if args.connection.startswith("udpin:") and (args.scan_baud or args.baud is not None):
        argument_parser.error("baud options apply only to serial")
    try:
        from pymavlink.dialects.v20 import ardupilotmega as dialect
    except ImportError as exc:
        print(f"PIXHAWK MAVLINK LINK: FAIL — pymavlink unavailable: {exc}")
        return 1
    selected_rates = BAUD_RATES if args.scan_baud else (args.baud or 115200,)
    failures = []
    try:
        for baud in selected_rates:
            try:
                if listen(args.connection, baud, dialect):
                    print("PIXHAWK MAVLINK LINK: PASS — received autopilot HEARTBEAT; passive receive check complete.")
                    return 0
                failures.append(f"{baud}: no autopilot HEARTBEAT")
            except (OSError, ValueError, ImportError) as exc:
                failures.append(f"{baud}: {exc}")
                # Opening/permission/dependency failures cannot be fixed by changing baud.
                break
    except KeyboardInterrupt:
        print("PIXHAWK MAVLINK LINK: FAIL — interrupted by operator.")
        return 130
    except Exception as exc:
        print(f"PIXHAWK MAVLINK LINK: FAIL — receive/parser error: {exc}")
        return 1
    print("PIXHAWK MAVLINK LINK: FAIL — " + "; ".join(failures))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
