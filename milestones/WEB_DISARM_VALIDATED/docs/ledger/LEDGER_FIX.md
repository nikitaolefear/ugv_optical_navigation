# Адресне виправлення ledger preflight — 17.11.2026

## Фактичний стан

Read-only SQLite snapshot, integrity_check=ok, FK без помилок:

| UUID | Стан | Причина | Write evidence |
|---|---|---|---|
| 6405cf1a-0b27-4e90-9916-e138142f1aa6 | SENT | DISARMED_STATE_OBSERVED_NOT_COMMAND_PROOF | FULL_WRITE |
| 2ad67346-699f-4073-afd0-8fc2604aa3ae | SENT | DISARMED_STATE_OBSERVED_NOT_COMMAND_PROOF | FULL_WRITE |

Ці транзакції завершені в сенсі engine.active=None: _retire() зберігає SENT,
фіксує причину й прибирає active. SENT є фактом повного serial write, а не
доказом виконання саме цієї команди. command_outcome лишається UNKNOWN.
QUEUED/SEND_ATTEMPT/WAITING та SENT/FULL_WRITE_NOT_COMMAND_PROOF відсутні.

Жодних DELETE/UPDATE/REPLACE production ledger під час діагностики не було.
Logical SHA256 на момент перевірки:
0503f25411e941e773cfddedc2d963e0db40ef43e46036ba13ca3d2f5eca8557.
Всю БД читали в mode=ro/query_only/BEGIN; status RPC не використовувався,
оскільки він може завершувати поточні observations і оновлювати ledger.

Початкове падіння сталося ДО будь-яких інсталяційних змін: target directory
і externalnav-observe.conf відсутні. Owner80074/gateway80106/auth80110 active,
sole owner80074 підтверджений; HMI login200, CONNECTED/freshDISARMED.

## Виправлення

1. ledger_policy.inspect_ledger() перевіряє цілісність, повноту delivery evidence,
   state/reason та узгодженість FULL_WRITE timestamps/serial. Pending,
   невідомі legacy states, невідомі причини або orphan/missing evidence
   відхиляються. Відомі термінальні UNKNOWN зберігаються як uncertainty,
   не оголошуються успіхом і ніколи не повторюються.
2. Preflight допускає непорожню завершену історію. Після зупинки gateway
   (нові RPC не приймаються) виконується повторна перевірка ДО stop owner.
   Якщо з'явилася pending команда — original owner не перезапускається,
   gateway повертається, встановлення припиняється. Copy/stage починається
   тільки після цієї перевірки.
3. Виявлено пов'язану втрату delivery-label у старому engine: constructor
   переписував усі SENT як UNKNOWN/OWNER_RESTARTED, навіть завершені. У candidate
   збережено відомі завершені SENT з FULL_WRITE. Pending/ambiguous контексти,
   як і раніше, стають UNKNOWN; self.active не відновлюється. Старий UUID
   повертає його наявний запис, без нового send. _observation() вже перевіряє
   owner_epoch, тому після restart не повертає старе DISARMED як свіже.
4. SQLite backup API робить приватну копію, але ніколи не відновлює її поверх
   поточного ledger. Повний logical SHA256 усіх таблиць (не лише commands)
   порівнюється після stop owner, із backup та після нового owner startup
   ДО відновлення gateway. Історичні UUID/reasons/evidence/identities/audit
   лишаються незмінними. Auth DB/key не мігруються й не змінюються installer.
5. Fallback при помилці після owner stop повертає старий owner profile без
   optical/diagnostic flags, але імпортує тільки вузько виправлений engine із
   candidate package, щоб старий constructor не переписав завершені SENT.
   Жодного автоматичного DB restore, cleanup команд або anti-replay reset.

## Виконані перевірки

`test_ledger_restart.py`: 12 вузьких перевірок на тимчасових SQLite-копіях:
реальні дві завершені SENT допускаються; QUEUED/SEND_ATTEMPT/WAITING,
SENT-still-observing, unsupported state/reason, missing/inconsistent evidence
відхиляються; відомий terminal UNKNOWN зберігає uncertainty; restart лишає
повний dump незмінним, включно з доданими historical audit/identities;
запити зі старими UUID не викликають sender; pending crash contexts лишаються
UNKNOWN без replay. Жодних physical FD, service operations або змін production DB.

Новий --check-ledger на чинній БД: LEDGER_IDLE, SENT2, terminal_uncertain0.
Загальні аудити/повний unit-цикл не повторювалися.

## Точні команди оператора

Перед install: ELRS DISARM, neutral throttle; не натискати Web DISARM під
час вікна встановлення. Gateway/owner короткочасно перезапускаються;
фізичний E-STOP та ELRS доступні оператору. HMI-процес не перезапускається.

```bash
cd /home/ugvs2/ugv_optical_navigation/externalnav_recovery_prepared
sha256sum -c SHA256SUMS
sudo /usr/bin/python3 -I "$PWD/install_observe.py" --check-ledger
sudo /usr/bin/python3 -I "$PWD/install_observe.py"
```

--check-ledger лише читає БД: очікується LEDGER_IDLE із SENT2 (або інша
завершена історія, яка пройшла перевірку). Якщо preflight відмовляє — не
очищати ledger і не примушувати restart.

Після успіху очікується OWNER_OPTICAL_OBSERVE_RUNNING,
ledger_preserved_at_restart=true. Новий owner має observe-only; ODOMETRY TX0.
Read-only PARAM_REQUEST_READ лишаються частиною погодженого diagnostics-профілю,
PARAM_SET/EKF/ARM/drive не використовуються.

Через20 секунд:

```bash
curl --noproxy '*' --max-time 3 -sS http://127.0.0.1:8084/api/externalnav
curl --noproxy '*' --max-time 3 -sS http://127.0.0.1:8084/api/owner-diagnostics
```

Після startup працюючий HMI може законно дописувати нові audit-події; це не
втрата історії. Нові ручні DISARM-запити після відновлення gateway також
законно змінюють ledger. Інваріант повного logical dump перевіряється в
закритому до нових команд вікні, а не вічно забороняє нормальну роботу.

Агент не запускав install і не перезапускав служби. Спосіб продовження —
оновлена команда install, не --apply/старі resume чи ручне виправлення SQLite.
