# UGV checkpoint — 2026-11-02

## Drive configuration

- Two driven wheels only: rear left and rear right.
- Front wheels are passive.
- Motor C / Throttle Left physically verified = left rear wheel.
- Motor D / Throttle Right physically verified = right rear wheel.
- Working DroneCAN configuration:
  - CAN_D1_UC_ESC_BM = 15
  - CAN_D1_UC_ESC_RV = 15
  - CAN_D1_UC_ESC_OF = 0
- Old tank_mixer.lua remains disabled.

## ExternalNav

- Rover 4.7.2
- ExternalNav Source Set 2 operational.
- VISION=1 required before autonomous tests.
- EKF origin:
  - LAT 50.0
  - LON 30.0
  - ALT 0
- AHRS_OPTIONS = 24
- Stored origin becomes active after switching to non-GPS Source Set 2.

## Turn-control incident

Original aggressive parameters:
- ATC_STR_RAT_MAX = 90 deg/s
- ATC_STR_ACC_MAX = 180 deg/s2
- ATC_STR_DEC_MAX = 0

During first 90-degree autonomous turn rover rotated much too fast,
overshot and collided.

DO NOT RUN THE OLD 90-DEGREE TURN SCRIPT AGAIN.

## Current conservative steering configuration

- ATC_STR_RAT_MAX = 5 deg/s
- ATC_STR_ACC_MAX = 5 deg/s2
- ATC_STR_DEC_MAX = 5 deg/s2
- ATC_STR_RAT_IMAX = 0.05
- ATC_STR_RAT_P = 0.2
- ATC_STR_RAT_I = 0.2
- GCS_PID_MASK = 1

FF tests:
- FF 0.2: wheels only loaded, no real turn
- FF 0.5: wheels only loaded
- FF 0.8: wheels only loaded
- FF 1.2: rover overcame static friction and started turning

## Critical FF=1.2 test result

Command:
- yaw-rate = -5 deg/s (left)
- requested stop at approximately 4 deg

Observed:
- TURN at ~1 s = -1.34 deg
- next observed TURN at ~2 s = -12.90 deg
- final after stop = -21.81 deg
- MAX ACHIEVED RATE = 16.22 deg/s
- MAX PID SUM = 0.1112

Important:
The attitude monitoring loop did not receive/process yaw frequently
enough to stop at 4 deg. Rover overshot badly after static friction
was overcome.

FF=1.2 must NOT be considered safe yet.

Parked configuration after testing:
- ATC_STR_RAT_FF returned to 0.8
- CAN should remain physically disconnected until next session.

## Next session

1. Do not test on ground initially.
2. Fix control-loop architecture:
   - independent high-rate attitude monitoring
   - command sending must not block yaw monitoring
   - explicit message-rate configuration
   - hard timeout
   - predictive/braking stop
3. Investigate why achieved yaw-rate reached ~16 deg/s despite
   ATC_STR_RAT_MAX = 5 deg/s.
4. Test first with rear wheels lifted.
5. Only after verified control:
   - very small ground turn
   - then marker transition during 90-degree turn.
6. Add new marker(s) for continuous visibility through a 90-degree turn.
