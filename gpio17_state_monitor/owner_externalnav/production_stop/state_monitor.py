"""Read-only view of existing Pi and Pixhawk telemetry."""
from .state import _finite_number


def _slot_voltages(telemetry):
    if telemetry.get('connection') != 'CONNECTED':
        return {}
    groups = (telemetry.get('esc_diagnostics') or {}).get('groups') or ()
    slots = {}
    # The reader publishes groups in receive order. Later packets supersede
    # earlier readings for the same slot, including a missing/zero reading.
    for group in groups:
        if (group.get('state') != 'FRESH' or
                group.get('matches_current_autopilot') is not True or
                group.get('source_filter_at_receive') != 'MATCH'):
            continue
        for esc in group.get('controllers', ()):
            number = esc.get('esc_number')
            if type(number) is int and 1 <= number <= 64:
                voltage = esc.get('voltage_v')
                slots[number] = voltage if _finite_number(voltage) and voltage > 0 else None
    return slots


def _reported_pwm(pwm, channel):
    value = pwm.get(f'servo{channel}_raw_us')
    return value if pwm.get('servo_state') == 'FRESH' and type(value) is int and value > 0 else None


class StateMonitor:
    """Publish telemetry without constructing or supplying motion evidence."""

    def __init__(self, read_brake_pwm=None, *, left_esc_slot=None, right_esc_slot=None):
        for slot in (left_esc_slot, right_esc_slot):
            if slot is not None and (type(slot) is not int or not 1 <= slot <= 64):
                raise ValueError('Invalid VESC slot')
        if left_esc_slot is not None and left_esc_slot == right_esc_slot:
            raise ValueError('VESC slots must be distinct')
        self.wheel_slots = {'left': left_esc_slot, 'right': right_esc_slot}
        self.read_brake_pwm = read_brake_pwm

    def snapshot(self, telemetry, stop_status):
        slots = _slot_voltages(telemetry)
        esc_slots = {str(slot): voltage for slot, voltage in sorted(slots.items())}
        wheels = {}
        for side, slot in self.wheel_slots.items():
            voltage = slots.get(slot)
            wheels[side] = {'esc_slot': slot, 'voltage_v': voltage,
                            'state': 'FRESH' if voltage is not None else 'UNKNOWN'}

        connection = telemetry.get('connection', 'UNKNOWN')
        pwm = {}
        if connection == 'CONNECTED' and self.read_brake_pwm is not None:
            pwm = self.read_brake_pwm() or {}
        return {
            'gpio17_front': stop_status['contacts']['FRONT'],
            'stop_latch': stop_status['stop_latch'],
            'mission_state': stop_status.get('mission_state', 'UNKNOWN'),
            'pixhawk': {'connection': connection,
                        'armed': telemetry.get('armed', {'state': 'UNKNOWN', 'value': None}),
                        'flight_mode': telemetry.get('flight_mode', {'state': 'UNKNOWN', 'value': None})},
            'brakes': {'physical_state': 'UNKNOWN', 'command': stop_status.get('brake_release', 'UNKNOWN'),
                       'servo5_pwm_us': _reported_pwm(pwm, 5), 'servo6_pwm_us': _reported_pwm(pwm, 6)},
            'vesc': {**wheels, 'esc_slots_voltage_v': esc_slots},
        }
