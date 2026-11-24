"""One in-memory compatibility check for the exact deployment candidate."""
from pathlib import Path
import shlex
import tempfile
from unittest.mock import Mock

from pymavlink.dialects.v20 import ardupilotmega as mav
from stop_latch_test.hold_runner import parse_args
from production_stop.owner_bridge import OwnerProductionStop
from production_stop.brake_link import BrakeLink
from disarm_control.inhibit import InhibitedControl, InhibitLatch
from web_hmi.pixhawk_telemetry import PixhawkTelemetry

line = Path('hold-only.conf.in').read_text().splitlines()[-1]
args = parse_args(shlex.split(
    line.split(' -m stop_latch_test.hold_runner ', 1)[1]
    .replace('${UGV_GATEWAY_UID}', '997').replace('${UGV_OWNER_RPC_GID}', '983')
))
assert args.brake_hold_only and args.externalnav_observe_only and not args.externalnav_tx
assert not args.web_arm_boot_id and not args.enable_bench_hold

with tempfile.TemporaryDirectory() as work:
    clock = Mock(return_value=10.0)
    reader = PixhawkTelemetry(clock=clock, opener=Mock(side_effect=AssertionError('serial opened')))
    assert callable(reader._run_with_reconnect)
    bench = Mock(disarm_pending=lambda: False)
    owner = OwnerProductionStop(reader, bench, str(Path(work) / 'stop.sqlite3'))
    engine = Mock()
    engine._heartbeat.return_value = None, Mock(base_mode=1), (1, 1), 1, 0
    inhibit = Path(work) / 'inhibit'
    inhibit.touch()
    guarded = InhibitedControl(engine, InhibitLatch(inhibit))
    guarded.tick(123)
    engine.set_enabled.assert_called_once_with(False)
    packets = []
    def write(fd, packet):
        assert fd == 123
        packets.append(mav.MAVLink(None).parse_char(packet))
        return len(packet)

    owner.brake = BrakeLink(reader, engine, owner, mav.MAVLink(None), lambda: None,
                            hold_only=args.brake_hold_only, write=write)
    owner.gpio_observed('RELEASED', clock())
    clock.return_value = 10.1
    owner.gpio_observed('PRESSED', clock())
    owner.brake.tick(123)
    view = owner.status(0, 'offline', '')
    assert view['stop_latch'] is True and view['contacts']['FRONT'] == 'PRESSED'
    assert not view['reset_available'] and not owner.brake.traction_ready()
    assert len(packets) == 1 and packets[0].get_type() == 'NAMED_VALUE_INT'
    assert packets[0].name == 'UGVBRAKE03' and packets[0].value == owner.brake.HOLD
    reader.opener.assert_not_called()
    owner.close()

print('PASS: reconnect owner + GPIO17 latch + HOLD frame only + inhibit + TX OFF')
