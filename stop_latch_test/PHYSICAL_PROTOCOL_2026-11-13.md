# Фізична перевірка GPIO17 / STOP_LATCH — 13.11.2026

Базовий коміт тесту: `2dc406738d2800c672bf7096fc810e37f51d92ce`.
Джерело: `~/stop-latch-test-20261113T171446Z.jsonl` на Raspberry Pi UGV №1.
Час у наведених рядках — UTC (`+00:00`); рядки скопійовано без змін.

Перевірено фізичний BCM GPIO17 (вхід із підтяжкою вгору, lo = натиснуто,
hi = відпущено) та логіку ізольованого тестового сервера **8084**.
Після відпускання START і RESUME відхилено за активного STOP_LATCH.
Ручний RESET о 17:21:33.026 прийнято: контакт RELEASED, блокування знято,
стан IDLE — автоматичного START немає. Повторне натискання о 17:22:35.800
встановило блокування; автоматична RESET-проба при PRESSED відхилена.
Після відпускання о 17:22:38.552 контакт RELEASED, але блокування лишилося
активним і тестовий стан STOPPED.

Фактичні рядки сесії (sequence 3–5, 8, 24–27; проміжні записи пропущено):

```jsonl
{"sequence": 3, "time": "2026-11-13T17:16:55.136+00:00", "event": "CONTACT_PRESSED", "detail": "", "contact": "PRESSED", "stop_latch": true, "test_state": "STOPPED", "reset_probe_armed": false, "test_only": true, "movement_allowed": false}
{"sequence": 4, "time": "2026-11-13T17:16:56.565+00:00", "event": "CONTACT_RELEASED", "detail": "", "contact": "RELEASED", "stop_latch": true, "test_state": "STOPPED", "reset_probe_armed": false, "test_only": true, "movement_allowed": false}
{"sequence": 5, "time": "2026-11-13T17:17:29.764+00:00", "event": "START_REJECTED", "detail": "TEST_UI: STOP_LATCH активний або контакт невідомий", "contact": "RELEASED", "stop_latch": true, "test_state": "STOPPED", "reset_probe_armed": false, "test_only": true, "movement_allowed": false}
{"sequence": 8, "time": "2026-11-13T17:17:59.024+00:00", "event": "RESUME_REJECTED", "detail": "TEST_UI: STOP_LATCH активний або контакт невідомий", "contact": "RELEASED", "stop_latch": true, "test_state": "STOPPED", "reset_probe_armed": false, "test_only": true, "movement_allowed": false}
{"sequence": 24, "time": "2026-11-13T17:21:33.026+00:00", "event": "RESET_ACCEPTED", "detail": "TEST_UI", "contact": "RELEASED", "stop_latch": false, "test_state": "IDLE", "reset_probe_armed": true, "test_only": true, "movement_allowed": false}
{"sequence": 25, "time": "2026-11-13T17:22:35.800+00:00", "event": "CONTACT_PRESSED", "detail": "", "contact": "PRESSED", "stop_latch": true, "test_state": "STOPPED", "reset_probe_armed": true, "test_only": true, "movement_allowed": false}
{"sequence": 26, "time": "2026-11-13T17:22:35.800+00:00", "event": "RESET_REJECTED", "detail": "AUTO_RESET_PROBE: RESET дозволений лише при відпущеному бампері", "contact": "PRESSED", "stop_latch": true, "test_state": "STOPPED", "reset_probe_armed": false, "test_only": true, "movement_allowed": false}
{"sequence": 27, "time": "2026-11-13T17:22:38.552+00:00", "event": "CONTACT_RELEASED", "detail": "", "contact": "RELEASED", "stop_latch": true, "test_state": "STOPPED", "reset_probe_armed": false, "test_only": true, "movement_allowed": false}
```

У всіх наведених записах `test_only=true`, `movement_allowed=false`.
Відмова RESET при натисканні тут підтверджена саме `AUTO_RESET_PROBE`,
а успішний ручний RESET — `TEST_UI`. Протокол не стверджує перевірку руху
чи надсилання команд Pixhawk. **Реальний HOLD на Pixhawk ще належить перевірити.**
Чинні служби під час збереження протоколу не змінювали й не перезапускали.
