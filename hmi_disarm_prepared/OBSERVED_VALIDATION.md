# Підсумок підготовки DISARMED_OBSERVED

Поточний профіль використовує штатний ArduPilot без firmware changes.
Немає CONFIRMED outcome для конкретної команди. Нова семантика визначена
README.md; старі AUDIT.md/TECHNICAL_DECISION.md зберігаються як історія
strict-ACK етапу, а не як чинний runtime contract.

## Що змінено

- `disarm_control/engine.py`: окремі delivery_state, observation_state,
  observation_pending; command_outcome завжди UNKNOWN. Fixed GCS 255/190,
  normal command400/param1=0/param2=0. ACK — лише uncorrelated diagnostics.
- `identities` не створюється новим engine і не змінюється, якщо існує.
  Немає allocation/reuse/reset. Additive delivery_evidence зберігає
  owner epoch, write baseline і повний/невизначений write. UUID і actor
  лишаються durable для ідемпотентності й контролю повторів.
- `disarm_control/gateway.py`: окремий explicit CLI gateway без hardware,
  subprocess або TCP; no retry, only DISARM/STATUS, fail-closed relay.
- `disarm_control/ipc.py`: точні cross-UID/GID/mode policy, peer credentials,
  nonce binding і inode checks; без послаблення schema/size/deadline guards.
- `disarm_control/inhibit.py`: root-managed one-way inhibit та owner facade,
  що перевіряється до request/status/tick, без автоматичного reenable.
- Prepared owner: gateway peer credentials обов'язкові, bench HOLD не
  ввімкнений за замовчуванням, 8084 mutation routes безумовно закриті.
  8082 telemetry та 8084 GET залишаються доступними.
- Prepared frontend: тільки gateway, CSRF/RBAC/audit/default deny, явні
  стан доставки/спостереження, foreground polling та local freshness expiry.
  Після невизначеності новий UUID можливий лише окремою дією оператора;
  повторного POST старого запиту немає.
- `deploy/`: нерозгорнуті мінімальні sandbox/config templates; feature OFF,
  UID/GID placeholders навмисно непридатні до запуску без provisioning.

## Перевірені властивості

1. Повний write не є виконанням; без post-write нового свіжого heartbeat
   немає здорового спостереження. ACK прийнятий/відхилений/пізній не є proof.
2. Stale, link loss, future/regressed/nonfinite clock, source change та
   повторний ARMED не залишають актуальний DISARMED.
3. Повтор UUID не дає другого write; restart/partial write/невідома доставка
   не спричиняють retry. Старі strict rows не повертають CONFIRMED.
4. Понад 254 нових запити не витрачають і не змінюють історичні identity rows.
5. Inhibit до send відхиляє queued intent; після send не приховує факт SENT,
   але не дозволяє видавати спостереження як справне. Audit failure під час
   disable лишає commands closed і не вимикає reader state.
6. 8084 GET не повертає CSRF control token; POST/PUT/PATCH/DELETE відхиляються,
   навіть із заголовками CSRF/Origin. Командний метод bench не викликається.
7. Gateway timeout/malformed reply/nonce mismatch/wrong credentials не
   вигадують прийняття або здоровий стан. Затримка врахована на gateway,
   HMI та в браузері, без нового timestamp для старого heartbeat.
8. Авторизація/CSRF/audit failure блокують dispatch. У жодної ролі немає
   автоматичного pixhawk.disarm. Poll transition audit обмежений і не
   пишеться кожні 500 мс без зміни стану.
9. UI подвійний клік не дає другий POST; втрата даних, прихована вкладка
   та timer expiry прибирають здорове спостереження. STATUS продовжується
   після першого DISARMED, тому re-ARM не залишається непоміченим у UI.
10. Наскрізний тест: авторизований Flask → ingress Unix gateway → backend
    Unix owner → fake writer → post-write heartbeat. ACK causal proof не потрібен.

## Результати

**253/253 Python-тести та 34/34 браузерні перевірки пройшли.**

| Набір | Пройшло |
| --- | ---: |
| Owner delivery/observations/MAVLink | 43 |
| IPC protocol | 14 |
| Gateway relay/policy | 14 |
| Inhibit latch | 4 |
| Static sandbox profile | 3 |
| Owner/HoldBench integration | 6 |
| Owner CLI validation | 3 |
| Legacy 8084 read-only | 5 |
| Auth/CSRF/audit/OBSERVED API | 25 |
| Flask → gateway → owner → fake writer | 1 |
| Auth regressions | 34 |
| Access regressions | 22 |
| DEMO regressions | 18 |
| Unchanged production STOP | 61 |

Browser: 17 перевірок у кожному viewport (1280×800 / 800×1280).
Артефакти: `/tmp/ugv-disarm-test-results.json`, `/tmp/ugv-disarm-browser/results.json` і PNG.


## Межі перевірки

Усі test DB/socket — disposable /tmp; actor permissions надаються тільки
тимчасовій test DB. Serial writer і GPIO джерела підмінені. Ніяких реальних
DISARM/HOLD/ARM/START, нових serial connections або GPIO reads у цьому етапі.
Не створено accounts/groups/services; template статично перевірений,
але реальні cross-UID DAC, systemd sandbox, helper/GPIO compatibility,
hardware latency, фактичний Sigma A1035 й поведінка VESC ще не перевірені.

Trust boundary: frontend service атестує actor claims після session/RBAC/CSRF;
gateway UID не є криптографічним доказом волі людини. Root/скомпрометований
довірений frontend чи owner не усунені самою IPC-ізоляцією. MAVLink signing
не додано; достовірність heartbeat спирається на чинний controlled owner/link.
DISARMED_OBSERVED не доводить причинність запиту або фізичну нерухомість.

Ніякі working HMI/HoldBench, logical roles, OS users, GPIO/Pixhawk/VESC,
systemd або фізичний E-STOP не змінені. `production_stop/` і його наявні
тести не переписані. Робочі служби перевірені read-only: active/running,
PID 960 та 1524 збережені. Identity DB не скидалася й не замінювалася.

Перед deployment потрібні review та cross-UID VM tests, погоджене
provisioning, consistent backup і окремий дозволений операторський стенд.
Поточний крок зупиняється перед deployment, staging, commit і push.
