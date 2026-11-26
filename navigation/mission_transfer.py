"""One bounded mission transaction on the owner's already-open descriptor."""

import json
import math
import os
import re
from pathlib import Path

from pymavlink.dialects.v20 import ardupilotmega as mav

TARGET = (1, 1)
MISSION = mav.MAV_MISSION_TYPE_MISSION
FIELDS = ('seq', 'frame', 'command', 'current', 'autocontinue', 'param1', 'param2',
          'param3', 'param4', 'x', 'y', 'z', 'mission_type')


def equivalent(expected, observed):
    if len(expected) != len(observed):
        return False
    for want, got in zip(expected, observed):
        keys = ('seq', 'command', 'mission_type', 'param1')
        if any(want[key] != got[key] for key in keys):
            return False
        if want['command'] == mav.MAV_CMD_NAV_WAYPOINT:
            # Rover canonicalizes global frames and stores altitude in cm.
            if (got['frame'] not in (mav.MAV_FRAME_GLOBAL, mav.MAV_FRAME_GLOBAL_INT) or
                    any(want[k] != got[k] for k in ('x', 'y')) or
                    not math.isclose(want['z'], got['z'], abs_tol=.011)):
                return False
    return True


class MissionTransfer:
    def __init__(self, encoder, directory, write=os.write):
        self.encoder, self.directory, self.write = encoder, Path(directory), write
        self.recipient = (encoder.srcSystem, encoder.srcComponent)
        self.phase = 'IDLE'
        self.error = None
        self.recovery = self.directory / 'mission-recovery.json'

    def start(self, items, request_id, actor_id, now):
        self.expected = list(items)
        self.backup = self.directory / ('mission-' + request_id + '.json')
        if self.backup.exists():
            raise ValueError('MISSION_REQUEST_ALREADY_CONSUMED')
        self.request_id, self.actor_id = request_id, actor_id
        self.expires = now + 30
        self.uploaded = False
        self.old_count = self.old_id = None
        self.recovery_baseline = None
        if self.recovery.exists():
            name = json.loads(self.recovery.read_text())['backup']
            if not re.fullmatch(r'mission-[0-9a-f-]{36}\.json', name):
                raise ValueError('Invalid recovery backup name')
            self.recovery_baseline = json.loads((self.directory / name).read_text())['items']
            if type(self.recovery_baseline) is not list:
                raise ValueError('Invalid recovery backup items')
        self._download('RECOVERY' if self.recovery_baseline is not None else 'BACKUP')

    def _download(self, stage):
        self.stage = stage
        self.phase = 'REQUEST_LIST'
        self.items = []
        self.count = self.plan_id = None
        self.inbox = None

    def fail(self, reason):
        self.phase, self.error = 'FAILED', reason

    def verified_readback(self):
        if self.phase != 'VERIFIED':
            return None
        return dict(item_count=len(self.items),
                    reverse_commands=[dict(seq=item['seq'], value=int(item['param1']))
                                      for item in self.items
                                      if item['command'] == mav.MAV_CMD_DO_SET_REVERSE])

    def _send(self, fd, packet, now, waiting):
        data = packet.pack(self.encoder)
        if self.write(fd, data) != len(data):
            raise OSError('Partial mission packet')
        self.encoder.seq = (self.encoder.seq + 1) % 256
        self.phase, self.deadline = waiting, now + 2

    def observe(self, msg):
        if (self.phase not in ('COUNT', 'ITEM', 'REQUEST', 'ACK') or
                msg.get_type() not in ('MISSION_COUNT', 'MISSION_ITEM_INT', 'MISSION_REQUEST', 'MISSION_REQUEST_INT', 'MISSION_ACK') or
                (msg.get_srcSystem(), msg.get_srcComponent()) != TARGET or
                (msg.target_system, msg.target_component) != self.recipient or msg.mission_type != MISSION):
            return
        if self.inbox is None:
            self.inbox = msg

    def _write_record(self, path, data):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        self._sync_directory()

    def _sync_directory(self):
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _save_backup(self):
        self._write_record(self.backup, dict(request_id=self.request_id, actor_id=self.actor_id,
                                            opaque_id=self.plan_id, items=self.items))

    def _clear_recovery(self):
        self.recovery.unlink()
        self._sync_directory()

    def _tick(self, fd, now, msg):
        if self.phase == 'REQUEST_LIST':
            self._send(fd, mav.MAVLink_mission_request_list_message(*TARGET, MISSION), now, 'COUNT')
        elif self.phase == 'COUNT' and msg is not None:
            if msg.get_type() != 'MISSION_COUNT' or not 0 <= msg.count <= 1024:
                self.fail('INVALID_MISSION_COUNT')
                return
            self.count, self.plan_id = msg.count, getattr(msg, 'opaque_id', None)
            self.phase = 'REQUEST_ITEM'
        elif self.phase == 'REQUEST_ITEM':
            if len(self.items) < self.count:
                self._send(fd, mav.MAVLink_mission_request_int_message(*TARGET, len(self.items), MISSION), now, 'ITEM')
                return
            self._send(fd, mav.MAVLink_mission_ack_message(*TARGET, mav.MAV_MISSION_ACCEPTED, MISSION),
                       now, 'DOWNLOAD_COMPLETE')
            if self.stage == 'BACKUP':
                self._save_backup()
                self.phase = 'UPLOAD_COUNT'
                self.next_seq = 0
            elif self.stage == 'RECOVERY':
                self._save_backup()
                if equivalent(self.expected, self.items):
                    self._clear_recovery()
                    self.phase = 'VERIFIED'
                elif self.recovery_baseline == self.items:
                    self._clear_recovery()
                    self.phase = 'RECOVERY_BASELINE_CONFIRMED'
                else:
                    self.fail('RECOVERY_MISSION_DIFFERS_RETAINED')
            elif equivalent(self.expected, self.items):
                self._clear_recovery()
                self.phase = 'VERIFIED'
            else:
                self.fail('MISSION_READBACK_MISMATCH')
        elif self.phase == 'ITEM' and msg is not None:
            if msg.get_type() != 'MISSION_ITEM_INT' or msg.seq != len(self.items):
                self.fail('INVALID_MISSION_ITEM')
                return
            self.items.append({name: getattr(msg, name) for name in FIELDS})
            self.phase = 'REQUEST_ITEM'
        elif self.phase == 'UPLOAD_COUNT':
            self._write_record(self.recovery, dict(backup=self.backup.name))
            self.stage = 'UPLOAD'
            self._send(fd, mav.MAVLink_mission_count_message(*TARGET, len(self.expected), MISSION), now, 'REQUEST')
        elif self.phase in ('REQUEST', 'ACK') and msg is not None:
            if msg.get_type() == 'MISSION_ACK':
                if self.phase == 'ACK' and msg.type == mav.MAV_MISSION_ACCEPTED:
                    self.uploaded = True
                    self._download('VERIFY')
                else:
                    self.fail('UPLOAD_NOT_ACCEPTED')
            elif (msg.get_type() in ('MISSION_REQUEST', 'MISSION_REQUEST_INT') and
                  (msg.seq == self.next_seq or self.next_seq > 0 and msg.seq == self.next_seq - 1) and
                  msg.seq < len(self.expected)):
                self.next_seq = max(self.next_seq, msg.seq + 1)
                waiting = 'ACK' if self.next_seq == len(self.expected) else 'REQUEST'
                self._send(fd, mav.MAVLink_mission_item_int_message(**self.expected[msg.seq]), now, waiting)
            else:
                self.fail('UNEXPECTED_UPLOAD_REPLY')

    def tick(self, fd, now):
        if self.phase in ('IDLE', 'FAILED', 'VERIFIED', 'RECOVERY_BASELINE_CONFIRMED'):
            return
        if now >= self.expires or (self.phase in ('COUNT', 'ITEM', 'REQUEST', 'ACK') and now >= self.deadline):
            self.fail(f'MISSION_PROTOCOL_TIMEOUT_{self.stage}_{self.phase}')
            return
        msg, self.inbox = self.inbox, None
        try:
            self._tick(fd, now, msg)
        except (OSError, ValueError):
            self.fail('MISSION_IO_OR_BACKUP_FAILURE')
