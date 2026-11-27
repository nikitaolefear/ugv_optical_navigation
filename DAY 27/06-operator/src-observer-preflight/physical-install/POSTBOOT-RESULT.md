# SRC Observer physical installation — postboot

**PASS. Active AHRS source set SRC1.** The operator confirmed physical installation, the four baseline checksums and both brakes mechanically applied without RELEASE. The configured USB device was detected and B2 drop-in, STOP DB and inhibit survived shutdown. No previous Observer attestation was present. The first service startup failed at Unix socket bind with EADDRINUSE. After verifying no process owned the serial link or socket, the stale socket was removed and the approved service start succeeded. One Owner, B2 and HMI active/running were confirmed.

Postboot readbacks confirmed LOG_DISARMED=0, SCR_ENABLE=1, SCR_HEAP_SIZE=204800 and MEMINFO freemem32=354976 bytes. The first new TIMESYNC RTT was 6.511 ms, with estimated uncertainty 8.256 ms. The approved boot-bound attestation was installed on the Pi after physical brake confirmation. The live observation used 35 refreshed time anchors.

The trusted window ran 60.371 s. Heartbeat age 0.006–0.996 s across 170 samples. HOLD, DISARMED, inhibit, STOP latch and PWM 999 microseconds on both brake channels held at all samples. ExternalNav was disabled with sent=0. ArUco marker 23 was fresh in all 170 samples and the pose counter increased by 2080. No Lua error or OOM appeared in the observed telemetry and no error-level service journal entry appeared after successful startup.

Captured 121 distinct S3SRC1 messages, source-set index 0, boot timestamps 193294–253322 ms, period 499–502 ms with median 500 ms and rate approximately 2 Hz. The first status sample was UNKNOWN while waiting for a qualifying message. The Owner then reported SRC1 in 169/170 samples, using its source 1/1, provenance, clock and evidence-age checks. Status reports SRC only while evidence receipt age is below one second. EKF_STATUS_REPORT remained fresh at flags=167 and LOCAL_POSITION_NED remained absent. The active AHRS source set gives no proof of ExternalNav EKF aiding.

No parameter writes, source changes, ExternalNav TX, ARM or brake release were performed. Private hardware identity, logs and host paths are excluded.
