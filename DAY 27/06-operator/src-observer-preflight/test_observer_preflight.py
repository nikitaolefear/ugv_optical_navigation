from pathlib import Path
import unittest
from zlib import crc32

from observer_preflight import (
    DIGEST, SIZE, TARGET, Refused, check_artifact, check_baseline,
    check_inventory, verify_readback,
)

APPROVED = Path(__file__).with_name("stage3_src_observer.lua").read_bytes()
CRC = crc32(APPROVED, 0xffffffff) ^ 0xffffffff


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_artifact_and_names(self):
        check_artifact(APPROVED)
        with self.assertRaises(Refused):
            check_artifact(APPROVED[:-1])
        check_inventory({"brake.lua": 1})
        with self.assertRaises(Refused):
            check_inventory({"STAGE3_SRC_OBSERVER.LUA": None})

    async def test_verified_read_and_fixed_path(self):
        async def read(parts, size):
            self.assertEqual((parts, size), (TARGET, SIZE))
            return APPROVED, DIGEST, CRC

        await verify_readback(read, APPROVED, lambda: True)

    async def test_partial_read_and_crc_fail_closed(self):
        for result in ((APPROVED[:-1], DIGEST, CRC), (APPROVED, DIGEST, None),
                       (APPROVED, DIGEST, CRC ^ 1), (APPROVED, "wrong", CRC)):
            async def read(*_):
                return result
            with self.subTest(result=result[1:]), self.assertRaises(Refused):
                await verify_readback(read, APPROVED, lambda: True)

    async def test_timeout(self):
        async def read(*_):
            raise TimeoutError
        with self.assertRaises(TimeoutError):
            await verify_readback(read, APPROVED, lambda: True)

    async def test_heartbeat_lost_before_or_during_read(self):
        called = []
        async def read(*_):
            called.append(True)
            return APPROVED, DIGEST, CRC
        for states, expected in (((False,), 0), ((True, False), 1)):
            guard = iter(states)
            called.clear()
            with self.assertRaises(Refused):
                await verify_readback(read, APPROVED, lambda: next(guard))
            self.assertEqual(len(called), expected)

    def test_baseline_exact_match(self):
        original = {("a",): 0, ("b",): 2, ("c",): 3, ("d",): 4}
        check_baseline(original, dict(original))
        edited = dict(original)
        edited[("a",)] = None
        with self.assertRaises(Refused):
            check_baseline(original, edited)
        with self.assertRaises(Refused):
            check_baseline(edited, edited)
