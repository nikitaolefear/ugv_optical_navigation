# Наступна інтеграція після WEB_DISARM_VALIDATED

Мета: статус переднього контактного бампера GPIO17, STOP_LATCH і захищений RESET у фактичному Web HMI, зі збереженням підтвердженого Web DISARM та штатного ExternalNav.

Факти на checkpoint:

- `/boot/firmware/config.txt` уже містить `gpio=17=ip,pu`; повторно встановлювати boot-конфіг не потрібно. Стара згадка «not installed» у `bumper17_boot_prepared` не відповідає чинному файлу.
- Чинний `hold_runner.py` використовує один monitor експериментального `StopLatch`; він читає BCM17 через `pinctrl get 17` (HIGH→RELEASED, LOW→PRESSED).
- Поточний Pi latch не є native interlock і не блокує ELRS тягу. HOLD вимкнений без `--enable-bench-hold`; legacy8084 лишається read-only. RESET у фактичному HMI стосується DEMO, production RESET API/кнопки ще немає.
- У репозиторії вже є `production_stop` core та пасивний GPIO adapter. Локальний `production_stop_integration` — лише candidate: один GPIO acquisition, owner bridge, peer-checked STOP_STATUS/STOP_RESET, CSRF/RBAC/audit і durable RESET UUID. Кнопок production RESET у ньому ще немає.
- Candidate base для `hold_runner.py` та `pixhawk_telemetry.py` застарів; payload не містить чинних ExternalNav/diagnostics модулів. Старий payload не можна встановлювати як повну заміну робочого пакета.

Найближчий крок: перенести вузькі candidate patches на код цього milestone; потім додати FRONT/STOP_LATCH status та RESET UI. До появи довірених native interlock/stationary/hold evidence RESET має лишатися недоступним, згідно з уже підготовленою логікою. Provider цих evidence зараз відсутній; наявний Pi latch не можна видавати за нього.

Цей файл — handoff. GPIO/serial не відкривали, тести не повторювали, candidate не встановлювали, код/служби/Pixhawk/навігацію не змінювали.
