# UGV Optical Navigation checkpoint — 2026-10-28

Working state:

- Raspberry Pi 5 + Pixhawk 6C + HM2131L.
- ArUco ExternalNav operational.
- Marker map v2: IDs 23, 24, 25.
- EKF Source Set 2 uses ExternalNav position and yaw.
- VISION=1 stable.
- ExternalNav watchdog timeout: 0.7 s -> HOLD + latch.
- Accelerometer calibration completed.
- Compass not used for EKF yaw.
- Old tank_mixer.lua disabled on Pixhawk:
  /APM/scripts/tank_mixer.lua.disabled
- Local backup:
  tank_mixer_from_pixhawk_BACKUP_20261028.lua
- SERVO1_FUNCTION=74
- SERVO2_FUNCTION=73
- SERVO3_FUNCTION=74
- SERVO4_FUNCTION=74
- CAN_D1_UC_ESC_BM=15
- CAN_D1_UC_ESC_RV=15
- Manual drive verified:
  forward / reverse / left / right OK.
- BATT_MONITOR=0 temporarily for testing.
- EKF origin required for GUIDED operation.
- Test EKF origin:
  lat=50.0
  lon=30.0
  alt=0.0 m
- GUIDED autonomous velocity control operational.
- Successful autonomous test:
  SPEED=0.30 m/s
  RUN_TIME=8.0 s
- Radio DISARM retained as emergency override.

Next:
- move by exact Optical/EKF coordinate instead of RUN_TIME;
- first target distance approximately 3 m;
- add stronger autonomous STOP/HOLD confirmation;
- add independent companion/heartbeat failsafe;
- restore/configure battery monitoring before production operation.

Important:
EKF origin was set using MAVLink.
Verify/re-set origin after Pixhawk reboot before GUIDED local-position operation.
