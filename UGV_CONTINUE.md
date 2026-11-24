# UGV — стан на 24.11.2026

- Встановлено HOLD-only + MAVLink reconnect. STOP_LATCH і DISARM inhibit активні, ExternalNav TX OFF.
- Pixhawk CONNECTED, Rover 4.7.2, MANUAL, DISARMED.
- ELRS працює, CH3 = 1503 мкс. SERVO5/6 = 999/999 мкс.
- Власник перевірив GPIO17, фізичний E‑STOP та утримання коліс після знеструмлення. Обидва VESC фізично відключені.
- FS_ACTION = 0, MODE_CH = 8. Запропоновані зміни ще не записані.
- Штатний RELEASE через CH8 із пріоритетом GPIO17 залишається BLOCKED.
- Резервна копія deployment: `/var/backups/ugv-hold-only-reconnect.8MsiqN`.

25.11.2026 почати з безпечного рішення для CH8 RELEASE/APPLY зі збереженням STOP_LATCH та production-блокувань. Уже пройдені перевірки не повторювати.

Актуальні ізольовані зміни StateMonitor, checkpoint та SITL збережено в `gpio17_state_monitor/`. Пакет deployment і матеріали статичного приймання містяться в `hold_only_reconnect/`.
