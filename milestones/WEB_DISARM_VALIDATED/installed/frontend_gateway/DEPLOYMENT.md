# Backup, enable/disable і rollback OBSERVED-профілю — не виконано

Це план для окремо погодженого вікна. Ніяких змін у поточних службах,
UID, permission DB, GPIO, Pixhawk, VESC або E-STOP не застосовано.
Штатний ArduPilot залишається без firmware/dialect змін. Адресований ACK
не є передумовою OBSERVED-контракту, але свіжий post-write HEARTBEAT потрібний.

## Перед дозволом на інтеграцію

1. Оператор забезпечує фізичну безпеку й незалежний E-STOP. Ні SENT,
   ні DISARMED_OBSERVED не означають фізично відключену тягу.
2. Review коду, поточних тестів та нової семантики: command_outcome завжди UNKNOWN.
3. Provisioning і sandbox у disposable VM: три UID, дві RPC-групи,
   root-owned code/venvs поза /home, private per-service state. Ніяких
   dialout/gpio/sudo у frontend/gateway, жодного успадкованого serial FD.
4. Legacy 8084 mutation closure перевірене; повертати тестовий POST у
   production не дозволяється. Нормальний owner без --enable-bench-hold.
5. Feature flags OFF та inhibit marker ON під час розгортання; жодних
   автоматичних POST/serial retries, ARM/RESET/START/RESUME.

## Backup фактичних файлів

Шляхи встановлені попередніми read-only перевірками; перед майбутнім вікном
повторно звірити effective unit/drop-in/env/WorkingDirectory.

| Об'єкт | Поточний шлях |
| --- | --- |
| Owner | `/home/ugvs2/ugv_optical_navigation/web_hmi/`, `stop_latch_test/` |
| Authorized HMI | `/home/ugvs2/ugv-hmi-auth-password8-release/web_hmi/` |
| Owner unit/drop-in | `/etc/systemd/system/ugv-hmi.service`, `ugv-hmi.service.d/hold-bench.conf` |
| Auth unit | `/etc/systemd/system/ugv-hmi-auth.service` |
| Auth state/key | `/home/ugvs2/.local/state/ugv-hmi/auth.sqlite3`, `auth.key` |
| Journals | у тому ж каталозі: `hold-bench.jsonl`, `telemetry-technical.jsonl` |
| Proxy | `/etc/nginx/conf.d/ugv-hmi.conf` |
| Python environments | `/home/ugvs2/.venv/`, `/home/ugvs2/.venv-ugv-hmi-auth/` |
| DISARM history | фактична owner DB, якщо на той момент існує; ніколи не підміняти тестовою |

У приватному backup поза Git (0700, секретні файли 0600) зберегти code,
unit/drop-in/effective env, owner/mode/symlinks/SHA256, dependencies і
записи про RBAC grants. Auth DB/ключ мають бути відповідною парою.
Для SQLite — backup API або погоджена зупинка writers; врахувати journal/WAL,
перевірити integrity_check на копії. Не друкувати ключі/секретний env.
Історичні identities та audit не обнуляти, навіть якщо новий профіль їх не витрачає.

## Конфігурація майбутнього профілю

- `deploy/` містить тільки нерозгорнуті приклади; UID/GID 0 означає
  незаповнений конфіг, який мусить fail closed.
- Auth 8083: нові UGV_DISARM_GATEWAY_SOCKET, UGV_DISARM_GATEWAY_UID,
  UGV_DISARM_FRONTEND_GID; немає fallback до прямого owner socket.
  UGV_HMI_ENABLE_DISARM=0 спочатку, UGV_HMI_TELEMETRY=0 завжди.
  Зберегти origin, Secure cookies, proxy policy та чинні ролі.
- Окремий ugv-command: тільки AF_UNIX, private DAC/peer policy, backend
  timeout, без пристроїв/привілеїв; --enable-disarm лише після review.
- Owner: тільки один чинний service/FD, explicit gateway peer UID/GID,
  backend socket, DB поза release і root-managed inhibit path. Не запускати
  паралельно старий і новий owner. Спочатку перевірити helper/GPIO залежності.
- У новій БД немає автоматичного pixhawk.disarm у жодній ролі. Надати
  лише погодженим ролям і анулювати відповідні сесії чинною процедурою.
- Production 8084 — read-only. Вимкнення його POST не вимикає 8082 телеметрію.

Міграція БД additive: delivery_evidence додає owner epoch/write baseline;
identities не змінюється. Незавершені старі контексти стають UNKNOWN,
старі strict CONFIRMED не видаються як нове причинне підтвердження.
Після зміни UID перевірити незмінність історичних записів, не створювати
порожню БД замість наявної. Upgrade/downgrade не є дозволом на replay.

## Безпечне вимкнення

У погодженій operations-процедурі root спочатку створює
`/run/ugv-disarm-policy/inhibit` у root:root 0755 directory без service write.
Gateway і owner latch OFF до restart. Недоступний/непридатний directory
також блокує. Queued до send відхиляється без write, SENT лишається фактом
доставки, observation стає UNKNOWN. Це не відкликання вже надісланого пакета
і не hard-real-time фізична зупинка. Telemetry/STATUS читання зберігаються.

Потім у погодженому вікні вимкнути frontend flag та gateway enable option.
Не використовувати рестарт як спосіб повторити невизначену команду.
Видалення inhibit marker саме по собі не вмикає поточні процеси.

## Відкат

1. Фізична безпека через E-STOP/оператора; заблокувати нові відправлення
   marker і feature flags, зберегти післяаварійний audit/current DB.
2. У погодженому вікні повернути перевірені frontend/gateway code/config.
   Owner бажано залишити в **новому telemetry-only/read-only профілі** без
   DISARM socket і без bench-hold flag: це зберігає телеметрію та закриття 8084.
3. Старий writable strict-ACK engine не запускати проти нового state для
   відновлення керування. Якщо release/state несумісні — DISARM OFF,
   telemetry-only, а не DB reset або auto-resend.
4. Не відновлювати стару identity DB/commands/auth DB поверх актуальних:
   зберегти post-deploy evidence/audit. Прибирати лише нові RBAC grants,
   повернути погоджені права/ownership і анулювати сесії без втрати audit.
5. Повний owner rollback, який повертає legacy control POST, потребує
   окремого maintenance рішення й закриття цього шляху до production.
   Не робити автоматичного firmware/GPIO/mode rollback.
6. Перевірити рівно одного serial-власника, свіжість телеметрії, UNKNOWN
   для недоступних спостережень, активний STOP_LATCH та відсутність нових writes.

Умови негайного disable/rollback: другий owner, неправильні UID/GID/mode,
legacy POST працює, replay UUID дав новий write, старий стан показаний як
свіжий, UI заявляє причинний успіх, audit/state/clock failure або
непередбачена поведінка приводів. Реальний стендовий TX — тільки окремо
погоджений етап після hardware-free і cross-UID VM перевірок.
