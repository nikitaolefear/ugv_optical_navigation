"""Offline checks for the pinned Observer. No transport or mutation entry point."""
from hashlib import sha256
from zlib import crc32

TARGET = ("stage3_src_observer.lua",)
SIZE = 341
DIGEST = "960f125c87258957cf07c2c13c5c83c4395f478fa0758fbffbd0263b98e49499"


class Refused(ValueError):
    pass


def check_artifact(data):
    if len(data) != SIZE or sha256(data).hexdigest() != DIGEST:
        raise Refused("observer does not match the approved artifact")


async def verify_readback(read_file, expected, guard):
    """Callback is the Owner's existing strict fetch_file with remote CRC."""
    check_artifact(expected)
    if not guard():
        raise Refused("guard unavailable")
    contents, digest, remote_crc = await read_file(TARGET, SIZE)
    if not guard():
        raise Refused("guard lost")
    check_artifact(contents)
    if contents != expected or digest != DIGEST:
        raise Refused("readback mismatch")
    if remote_crc != (crc32(contents, 0xffffffff) ^ 0xffffffff):
        raise Refused("remote CRC missing or invalid")


def check_inventory(entries):
    """Block if the root name already exists, including FAT case aliases."""
    if any(name.casefold() == TARGET[0].casefold() for name in entries):
        raise Refused("observer name occupied")


def check_baseline(before, after):
    """Mappings of the four private baseline paths to verified remote CRCs."""
    if (len(before) != 4 or before != after
            or any(type(crc) is not int or not 0 <= crc <= 0xffffffff for crc in before.values())):
        raise Refused("baseline changed or incomplete")
