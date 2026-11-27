"""Fixed MAVLink packet builders for a future Owner-local queue adapter."""
from pymavlink.dialects.v20 import ardupilotmega as mav

from log_disarmed import PARAMETER, Refused

TARGET = (1, 1)
WIRE_NAME = PARAMETER.encode("ascii")


class LogDisarmedPort:
    """No connection opener. The existing Owner must implement exchange.

    exchange(message, check, timeout, accept) must run check on the sole
    Owner thread immediately before sending. With no accept callback it
    returns after confirmed local write, without treating PARAM_VALUE as ACK.
    With a callback it returns the first matching message in a fresh receive
    epoch after dispatch. The Owner must drain or quarantine earlier replies
    and serialize parameter traffic. Expired work may never be sent.
    """

    def __init__(self, exchange, identity, safety):
        self.exchange, self.identity, self.safety = exchange, identity, safety

    def read_log_disarmed(self, check):
        def accept(msg):
            return (msg.get_type() == "PARAM_VALUE"
                    and (msg.get_srcSystem(), msg.get_srcComponent()) == TARGET
                    and msg.param_id == PARAMETER
                    and msg.param_type == mav.MAV_PARAM_TYPE_INT8
                    and msg.param_value in (0, 1, 2, 3))

        msg = self.exchange(mav.MAVLink_param_request_read_message(*TARGET, WIRE_NAME, -1),
                            check, 2.0, accept)
        if msg is None or not accept(msg):
            raise Refused("fresh LOG_DISARMED readback missing")
        return int(msg.param_value)

    def write_log_disarmed(self, value, check):
        if type(value) is not int or value not in (0, 1, 2, 3):
            raise Refused("invalid LOG_DISARMED value")
        self.exchange(mav.MAVLink_param_set_message(*TARGET, WIRE_NAME, value, mav.MAV_PARAM_TYPE_INT8),
                      check, 2.0, None)
