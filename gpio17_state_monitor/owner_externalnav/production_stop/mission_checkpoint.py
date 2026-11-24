"""Checkpoint data from a complete, coherent MISSION_ITEM_INT download."""
from dataclasses import dataclass
from hashlib import sha256
import math
import struct

ROUTE_MAX_AGE = .5


@dataclass(frozen=True)
class RouteSnapshot:
    observed_at: float
    items: tuple
    started_at: float | None = None
    opaque_id: int | None = None

    def fingerprint(self):
        """ArduPilot's dynamic HOME (seq 0) is excluded from route identity."""
        if len(self.items) < 2:
            raise ValueError('Mission has no route items')
        digest = sha256(b'ardurover-mission-item-int-v1\0')
        for seq, item in enumerate(self.items):
            if (item.get_type() != 'MISSION_ITEM_INT' or item.seq != seq or
                    item.mission_type != 0):
                raise ValueError('Incomplete or non-mission route')
            if seq == 0:
                continue
            params = tuple(getattr(item, f'param{index}') for index in range(1, 5))
            if any(math.isinf(value) for value in (*params, item.z)):
                raise ValueError('Infinite mission parameter')
            digest.update(struct.pack('>HBHBii', seq, item.frame, item.command,
                                      item.autocontinue, item.x, item.y))
            for value in (*params, item.z):
                digest.update(bytes.fromhex('7fc00000') if math.isnan(value)
                              else struct.pack('>f', value if value else 0.0))
        return digest.hexdigest()


@dataclass(frozen=True)
class MissionCheckpoint:
    stop_id: str
    stopped_at: float
    route_sha256: str
    seq: int

    def journal_details(self):
        return {'stop_id': self.stop_id, 'waypoint_seq': self.seq, 'route_sha256': self.route_sha256}
