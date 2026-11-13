# Статистика періодів v2 — семантика та міграція

Це кандидат, не live release. Приватну БД, старий кандидат, робочу HMI,
ugv-hmi-auth-release, службу та Pixhawk у цьому сеансі не змінювали.
Усі SQL-тести виконані тільки на синтетичних тимчасових БД.

## Правила підрахунку

- Періоди: поточні календарні година, день, ISO-тиждень з понеділка,
  місяць, рік у Europe/Kyiv та весь час від coverage_from. Межі [start,end).
- started належить часу START.
- completed, cancelled, failed, interrupted належать збереженому END.
  Місія через межу періоду додає старт у старий період, результат — у новий.
  Результатів у періоді може бути більше за стартів; це не помилка.
- active — поточний COUNT(ACTIVE) із ledger, з урахуванням прав/ініціатора,
  незалежно від обраного періоду. Він НЕ дорівнює різниці періодних counters.
- Ініціатором завжди лишається акаунт, що підтвердив START. Завершення іншим
  оператором, зміна ролі чи деактивація не переносять статистику на інший акаунт.
- Частка успішних серед завершених результатів обраного періоду:
  completed / (completed + cancelled + failed + interrupted). Включені всі
  чотири термінальні стани; active виключено. Якщо знаменник нуль або період
  має неповну часову історію — success_rate_pct=null, у UI «—».

END у цій HMI — зафіксований час першої реєстрації термінального стану,
не незалежно виміряний фізичний момент завершення. DEMO як і раніше
обчислює прогрес при snapshot/command. Якщо БД тимчасово недоступна,
Simulator зберігає pending END разом із результатом; повторний запис
не переносить результат у нову годину. Після crash невідомий час аварії
не вгадується: для INTERRUPTED END є часом виявлення/закриття при recovery.
Це особливо важливо для історії v1 — її saved ended не переписується.

Права не змінено: operator — власний журнал/статистика; admin — усі та
by_operator/фільтр оператора. Без сесії APIs →401 JSON, недостатня роль →403.
DEMO/REAL розділені; producer REAL як і раніше не підключений, summary=null.

## Версія схеми

Версія компонента — mission_meta.version, не загальна auth-схема.
Нова порожня БД одразу отримує version=2. V1 зберігала ВСІ counters у годині
START, тому просто перейменувати поля/змінити метадані недостатньо.
Міграція 1→2 виконується один раз у SQLite BEGIN IMMEDIATE:

1. Отримати flock hmi-engine.lock. Якщо підтримуваний старий HMI ще живий
   і тримає lock — відмовити, транзакцію відкотити. Все одно потрібна повна
   погоджена зупинка старого процесу: не оновлювати схему під живим сервером.
2. Перевірити all-time totals, ACTIVE ledger і retained результати. Некоректні
   від'ємні/суперечливі counts або missing all-time row — відмова, не виправлення
   припущенням. Version і агрегати залишаються v1.
3. Залишити all-time hour=-1 ЗА ЗНАЧЕННЯМИ незмінним. Не додавати
   до нього результати з ledger ще раз. START-погодинні counts теж не зсувати.
4. Обнулити лише старі погодинні result counters і перебудувати їх за
   збереженими скінченними mission_runs.ended. UTC buckets правильно
   відокремлюють повторену осінню годину; вікна відображаються за Києвом.
5. Для domain/ініціатора обчислити різницю all-time results мінус retained
   results зі збереженим END. Записати її в mission_history_gaps. Це counts
   із невідомим часом; їх не приписувати ні START, ні migration_at, ні нулю.
   Не відновлювати час із audit, тривалості маршруту, порядку UUID чи приблизної дати.
6. Записати result_time_migration_at та version=2 разом з усіма змінами.
   Будь-яка помилка, у т.ч. переповнення БД, відкочує всю транзакцію.

Повторна v2 ініціалізація не перебудовує counters. Конкурентні ініціалізатори
серіалізовані SQLite: лише один виконує 1→2. Невідома версія або відсутня
версія непорожньої БД не інтерпретується автоматично.
Users, password hashes, key і sessions міграція не переписує. Новий pending
END існує лише в RAM поточного Simulator; для втраченої історії він не генерується.

## Втрачені часи і повнота звітів

Якщо ledger вже очищено, saved all-time still точний за старими totals,
але частину результатів неможливо розмістити в календарі. Для такого scope:

- sources.<domain>.history_without_end_time містить кількість за статусами;
- all_time показує всі збережені totals і точний відсоток за ними;
- період, що міг містити ці старі результати, має summary.results_complete=false;
  його counters — ВІДОМА ЧАСТИНА, не гарантовані повні нулі;
- success_rate_pct у неповному періоді null; UI показує попередження;
- період із початком строго ПІСЛЯ result_time_migration_at має повний новий
  часовий облік. Це консервативна межа, а не вгадування дат втрачених результатів.

Повнота обчислюється окремо для кожного оператора. Невідома історія іншого
користувача не робить власний звіт operator неповним і не розкриває його дані.
Admin global summary може бути неповним, а окремий grouped operator — повним.
Часові метадані припускають коректний серверний UTC-годинник; міграція не
може виправити невідомі попередні зсуви системного часу.

Нові v2 фінали записуються один раз у all-time та у власну END-годину.
START аналогічно один раз у all-time та START-годину. При повторному finish
перший успішно збережений END не змінюється. Після pruning ledger v2 підсумки
не перебудовуються — збережені counters продовжують бути джерелом звітів.
Retention: hourly 400 днів, ledger 90 днів/10000 фіналів, ACTIVE не чиститься,
all-time та migration gaps не видаляються. Ліміт приватної SQLite лишається.

## Майбутній rollout — команди НИЖЧЕ НЕ ВИКОНАНО

Не використовувати робочу БД для тестів. Спершу погоджено зупинити старий
процес, перевірити його завершення і підготувати приватний offline backup.
Приклад backup від ugvs2, без секретів у виводі (шляхи звірити з EnvironmentFile):

```bash
# ТІЛЬКИ після окремого погодження зупинки HMI, не зараз.
# Запускати з reviewed release; перед цим перевірити реальний STATE path.
/home/ugvs2/.venv/bin/python - <<'PY'
from datetime import datetime
from pathlib import Path
import os, shutil, sqlite3
os.umask(0o077)
state_dir = Path('/home/ugvs2/.local/state/ugv-hmi')
backup_root = Path('/home/ugvs2/.local/state/ugv-hmi-backups')
backup_root.mkdir(mode=0o700, exist_ok=True)
backup_dir = backup_root / ('before-periods-v2-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
backup_dir.mkdir(mode=0o700, exist_ok=False)
assert (state_dir/'auth.sqlite3').is_file() and (state_dir/'auth.key').is_file()
source_uri = (state_dir/'auth.sqlite3').resolve().as_uri() + '?mode=ro'
with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(backup_dir/'auth.sqlite3') as target:
    source.backup(target)
shutil.copy2(state_dir/'auth.key', backup_dir/'auth.key')
for name in ('auth.sqlite3','auth.key'): (backup_dir/name).chmod(0o600)
print(backup_dir)  # тільки шлях, не користувачі/сесії/ключі
PY
```

Перевірити backup, зробити ще одну ОКРЕМУ приватну копію для rehearsal.
На rehearsal, не на state служби, запустити код нового кандидата:

```bash
# disposable_dir має бути НОВОЮ копією backup, 0700, поза repo/release.
# Ні web_hmi.app factory, ні telemetry start тут не викликаються.
/home/ugvs2/.venv/bin/python - <<'PY'
from pathlib import Path
from web_hmi.auth import AuthStore
from web_hmi.mission_statistics import MissionStatistics
copy_dir = Path('/home/ugvs2/.local/state/ugv-hmi-migration-rehearsal')
assert (copy_dir/'auth.sqlite3').is_file() and (copy_dir/'auth.key').is_file()
assert copy_dir.resolve() != Path('/home/ugvs2/.local/state/ugv-hmi').resolve()
store = AuthStore(copy_dir)
with store.transaction() as db:
    before = [tuple(r) for r in db.execute('SELECT * FROM mission_counts WHERE hour=-1 ORDER BY domain,operator_id')]
statistics = MissionStatistics(store)
with store.transaction() as db:
    after = [tuple(r) for r in db.execute('SELECT * FROM mission_counts WHERE hour=-1 ORDER BY domain,operator_id')]
    assert db.execute("SELECT value FROM mission_meta WHERE key='version'").fetchone()[0] == '2'
assert before == after
print('rehearsal: schema v2, all-time unchanged')
PY
```

У цьому сеансі цей приклад не запускався. Приклад backup не означає дозвіл
на доступ до приватної БД. Перед реальним запуском перевірити копію/шляхи
та штатний механізм зупинки/backup на хості. Новий код сам виконує upgrade
під час approved factory startup; не запускати його factory проти live state
до цього кроку. Не змінювати вручну mission_meta.version.

Після rehearsal і погодження rollout: повністю stop старого HMI → offline
backup → reviewed v2 release → один production worker із тим самим state →
перевірка schema_version=2 і quality flags → перезавантаження сторінок
операторів, щоб старі відкриті вкладки не показували v1 пояснення нових counters.
HTTPS/owner helper/один serial reader лишаються за README_AUTH.md; тут нічого
з цього не запускали, пакетів/служб/системних конфігурацій не змінювали.

## Відкат

Попередній статистичний v1 кандидат відмовляється працювати з version=2.
Перемикання лише source release назад недостатньо. НЕ підмінювати 2 на 1:
END-агрегати вже мають інший зміст, а з pruned history немає коректного reverse.

Краще виправлення вперед на окремій копії. Якщо потрібен саме v1 rollback:
погоджено stop v2 → приватний backup поточного v2 state для розслідування →
узгоджене відновлення offline v1 auth.sqlite3 + auth.key → повернення v1 release
→ перевірка admin/ролей → відкликання відновлених старих сесій локальним CLI
→ погоджений запуск. Зміни акаунтів, сесій та місій після v1 backup можуть
бути втрачені; це треба явно прийняти до restore. Два процеси паралельно
не запускати. Auth-only release не розуміє статистику, її не веде — це розрив
обліку, не спосіб перетворення v2 на v1. Приватний state не вкладати в пакет.

## Перевірки та обмеження

Нові тести перетинають межі години/дня/тижня/місяця/року і весняний та осінній
DST Europe/Kyiv для всіх чотирьох terminal outcomes. Окремо перевірено active
незалежно від періоду, START-ініціатора, replay/concurrency, all-time без дублювання,
повний/pruned/NULL-END v1 ledger, міграцію/rollback, live engine lock, права scope,
а також збережений час END при повторному записі після помилки БД.

Результат повної перевірки — test-results.txt у корені кандидата. Справжні
Gunicorn/nginx/HTTPS, планшет, production БД та реальні місії НЕ перевірялися.
Міграція тестувалася тільки на синтетичних temp БД. REAL source не підключений.
