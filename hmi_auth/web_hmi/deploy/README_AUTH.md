# Авторизація UGV HMI — окрема копія для перевірки

У цьому сеансі НЕ змінено робочу службу ugv-hmi, вихідну web_hmi,
WireGuard, sudoers чи параметри Pixhawk. Немає встановлення пакетів,
перезапуску серверів, відкриття tty або мережевих портів.
Робоча копія розробки: /tmp/ugv-hmi-auth-review. Остаточний кандидат і всі
артефакти збережено в новому каталозі під
/home/ugvs2/ugv_optical_navigation_candidates/; точний шлях див. у
FINAL_LOCATION.txt і REPORT.md у корені кандидата. Існуючі копії не замінюються.

## Доступ і ролі

Немає стандартних облікових записів, паролів, реєстрації чи обходу auth.
Першого admin створює локальний CLI з getpass. Логіни нормалізуються до
нижнього регістру, 3–64 ASCII символи [a-zA-Z0-9_.-]. Паролі 8–128 символів включно,
зберігаються як salted scrypt:32768:8:1 (приблизно 32 MiB пам'яті на хеш).
Немає вимог лише цифр чи обов’язкових класів: дозволені літери, цифри,
пробіли та спецсимволи. Пробіли враховуються в довжині й не видаляються;
пароль не обрізається та не нормалізується. Політика перевіряє Python Unicode
символи (code points). Login UI не обрізає введення через maxlength: сервер
відхиляє перевищення межі. Вхід зі старими збереженими хешами не змінено.
Werkzeug використовується як наявна залежність Flask; новий crypto-пакет
не потрібен. Тестові облікові записи створюються лише test_support.py.

| Можливість | operator | admin |
|---|---|---|
| HMI, статус DEMO, пасивна телеметрія | так | так |
| Команди лише DEMO | так | так |
| Власний журнал /api/audit | так | так |
| Усі журнали /api/admin/audit | 403 | так |
| Власна статистика /api/statistics | так | так |
| Загальна статистика /api/admin/statistics та вибір оператора | 403 | так |
| Список користувачів /api/admin/users, без хешів | 403 | так |
| Зміни користувачів/ролей | лише локальний CLI | лише локальний CLI |

Усі /api/*, включно з майбутніми endpoint, мають централізовану перевірку
сесії: анонімний запит → JSON 401. Недостатня роль → JSON 403.
Сторінки /, /audit, /statistics й application JS потребують входу. Публічні лише
/login та /static/style.css для сторінки входу. GET logout відсутній.
Роль береться з SQLite на кожному запиті, а не з cookie чи браузера.
Відкликання блокує наступні запити; уже дозволений запит у виконанні
не скасовується. Це стосується тільки DEMO, реального керування немає.

Оператор не отримує спільний Simulator.events через API: events=[] у
статусах і відповідях команд. Замість спільного журналу інтерфейс показує
персональний audit. Спільний стан DEMO залишається видимим обом ролям.
Адміністратор бачить невдалі входи, зміни акаунтів, запуск/зупинку HMI,
запуск телеметрії та переходи CONNECTION. Входи і дії мають час, актора,
роль на момент дії, дію та результат. Зміни локального CLI позначені
local_uid:<uid>, роль local_admin. Час у БД — UNIX/UTC, у UI — Europe/Kyiv.

Невдалі входи не приписуються перевіреному користувачу і доступні тільки
admin. Сирий введений логін у failed-login журнал не потрапляє: це може
бути випадково введений пароль. Ні паролі/хеші, ні cookie, CSRF, ключі,
request body чи query string не пишуться в audit. Довільне поле action
команди журналюється як demo.invalid, без введеного значення.

## Сесії, CSRF, обмеження входів

Cookie ugv_session містить випадковий 256-bit opaque token. У SQLite
зберігається тільки його HMAC-SHA256 під зовнішнім ключем. Це власні
серверні сесії, не Flask signed client-side session. CSRF — окремий
випадковий 256-bit synchronizer token, збережений у приватній БД.
Після входу token і CSRF змінюються; анонімна сесія видаляється.

Idle timeout — 15 хв, абсолютний максимум — 8 год. Анонімна login-сесія
діє до 5 хв. Перехід годинника назад також закриває сесію. Logout видаляє
серверну сесію й cookie. Зміна ролі, пароля, active чи CLI revoke видаляє
всі сесії відповідного користувача. Читання /api/status, /api/telemetry,
/api/session та журналів саме по собі НЕ продовжує idle timeout.
Реальні натискання/ввід у браузері надсилають CSRF-захищений activity POST
не частіше разу на хвилину; навігація HMI/журналом і DEMO-команди також
рахуються як активність. Статус UI не є джерелом серверного дозволу.

Cookie: HttpOnly, SameSite=Strict, Path=/, без Domain; Secure обов'язковий
для HTTPS. Усі зміни, включно з login, logout і activity, потребують CSRF.
Якщо Origin передано, він мусить точно дорівнювати PUBLIC_ORIGIN;
Sec-Fetch-Site=cross-site відхиляється. Токени не передаються в URL.
Host обмежений PUBLIC_ORIGIN. CSP дозволяє лише same-origin ресурси та
форми, забороняє frame і base. Cache-Control=no-store, Referrer-Policy
no-referrer. HSTS додається на HTTPS.

Стійкі до перезапуску ліміти в SQLite: 5 спроб/15 хв на нормалізований
логін та 20/15 хв на IP. Резервація спроби перед дорогим хешуванням
обмежує також паралельні спроби. Успішний вхід скидає ліміт логіна, але
не IP. Неправильний пароль, невідомий/неактивний акаунт мають однакову
помилку; rate limit повертає 429 з тим самим текстом. Unknown login теж
перевіряє scrypt dummy hash. Nginx додає окремий грубий IP-ліміт.

Завершення auth-сесії/logout НЕ викликає STOP або інші команди: серверна
DEMO-симуляція може продовжуватися. Телеметрія і далі лише пасивна.

## Приватне сховище та межі довіри

За замовчуванням /home/ugvs2/.local/state/ugv-hmi, поза репозиторієм,
static і web-root. Каталог 0700, поточний UID; auth.sqlite3 та auth.key —
0600, поточний UID, звичайні файли без symlink. Розміщення в source tree,
небезпечні права або writable group/other батьківські каталоги відхиляються
(захищений sticky /tmp дозволений для тестів). Ключ 32 байти генерується
локально при першому відкритті сховища; у репозиторії секретів немає.
Flask від root відмовляється запускатися. ОС-акаунт ugvs2 з доступом до
БД є довіреним локальним адміністратором: application roles не ізолюють
від власника Unix-файлів. Не запускати неперевірений код під ugvs2.

Журнал: максимум 10 000 рядків і 30 днів, що настане раніше. Очищення на
кожній вставці, CLI prune також запускає очищення. Події старші за політику
видаляються, гарантії мінімального зберігання немає. За потреби експортувати
їх до видалення лише адміністративним інструментом у приватне сховище.
Максимум 200 анонімних login-сесій, 1000 authenticated sessions і 5000
лімітерів. SQLite max_page_count=16384 (приблизно 64 MiB при page_size 4096);
тимчасовий rollback journal може вимагати додаткове місце. Видалені записи
залишають вільні сторінки для повторного використання; файл не роздувається
безмежно. Offline VACUUM/приватні backup робити лише за окремим планом.
Сховище не є незмінним або криптографічно захищеним журналом від власника ОС.
Nginx-логи повинні мати distro logrotate (перевірити охоплення нових імен),
наприклад daily, rotate 7, maxsize 10M; не вмикати debug або логування headers/body.

Перед DEMO-командою зберігається PENDING; при успіху/відмові — OK/REJECTED.
Якщо сховище не працює до цього запису, команда не виконується. Якщо запис
підсумку не вдався ПІСЛЯ зміни DEMO, клієнт отримує 503, у журналі лишається
PENDING. SQLite і симулятор у RAM не мають спільної атомарної транзакції:
перевірити стан перед повтором, зберегти request_id/revision політику v0.2.
Результат PENDING не означає, що команда не виконалася.

## Статистика місій — схема v2

Періоди hour/day/week/month/year/all_time — поточні календарні періоди
Europe/Kyiv, не rolling інтервали. Тиждень починається в понеділок, межі
[start,end), DST врахований, повторені осінні години мають різні UTC межі.
All-time — від coverage_from, історія до початку обліку не вигадується.

Прив'язка до акаунта завжди за ініціатором підтвердженого START, навіть
коли інший оператор завершує/скасовує місію. Admin також може бути
ініціатором; зміна ролі/active не стирає його історію.

- started — за збереженим часом START.
- completed/cancelled/failed/interrupted — за збереженим часом END, незалежно
  від того, в якому періоді місія почалася.
- active — поточна кількість ACTIVE у ledger, незалежно від вибраного періоду.
  Це gauge, а не started мінус результати обраного періоду. Результатів у
  періоді може бути більше за стартів; це нормально.
- Частка успішних серед завершених результатів обраного періоду:
  completed/(completed+cancelled+failed+interrupted). Active не входить.
  За відсутності результатів або неповної часової історії — null / «—».

START створює одну спробу; PAUSE/RESUME її не дублюють. Повторний request_id
чи polling не створюють місій/завершень. STOP/RESET/HOLD до START не рахуються,
після START — cancelled. FAULT/E_STOP — failed, COMPLETE — completed.
Незавершені спроби після втрати RAM стають interrupted, а не вигаданим успіхом.
Їх END — час реєстрації термінального стану при recovery, не вгаданий час аварії.
Нормальний END — час першого спостереження термінального стану; при помилці
запису цей час зберігається в pending і не зсувається на час повторної спроби.
Фізичний час завершення реального руху з read-only телеметрії не визначається.

Stats API /api/statistics — тільки власні дані; чужий operator_id/scope=all →403.
/api/admin/statistics — усі підсумки та by_operator/фільтр акаунта. Без сесії
JSON 401, operator на admin API →403. Poll статистики не продовжує idle timeout.
Окрема сторінка /statistics пояснює різні часи лічильників та current active.

Таблиці mission_runs/mission_counts/mission_meta зберігаються у приватній
SQLite поза source/static. Погодинні агрегати: 400 днів, деталізований
ledger: 90 днів/10000 завершених записів; ACTIVE не видаляються, максимум
1000. All-time (-1 bucket) зберігається незалежно від цих очищень та audit.
Ліміт SQLite ~64 MiB лишається. За браку місця START без обліку не виконується.
Підсумок завершення і counters — одна транзакція; повторний finish ідемпотентний.

Схема mission_meta.version=2. V1 помилково прив'язувала всі результати до START.
Міграція зберігає all-time та START-агрегати, перебудовує END-агрегати лише
за збереженим ledger.ended і не читає audit для вгадування часу. Втрачені
часи обліковуються окремо в mission_history_gaps за domain/ініціатором.
Для потенційно неповних періодів summary.results_complete=false, результати
є лише підтвердженою частиною, відсоток не обчислюється. All-time лишається
точним за збереженими totals. Період, який почався строго після migration_at,
не може містити старих v1 результатів і знову має повний часовий облік.

Міграція атомарна, серіалізована SQLite BEGIN IMMEDIATE та додатковим
hmi-engine.lock. Якщо старий HMI тримає lock або totals суперечать ledger,
вона відмовляється без зміни версії/агрегатів. При повторному запуску v2
міграція не повторюється. Невідома/відсутня версія непорожньої схеми
не інтерпретується автоматично. Users, hashes, key, sessions не переписуються.

DEMO/REAL мають різні counters. У поточному продукті REAL producer не
підключений: available=false, summary=null. Це UNKNOWN, а не нуль доведених
місій. Синтетичний REAL у тестах перевіряє лише розділення обліку.

Детальна процедура offline backup, migration/rollback та межі відновлюваної
історії: README_PERIODS_V2.md. Приватну БД робочої служби в цьому сеансі
не відкривали і не мігрували; усі перевірки працюють із тимчасовими fixtures.

## Локальний CLI — виконувати від ugvs2, без sudo

Команди наведені для майбутнього встановлення; у цьому сеансі не виконані.
Пароль вводиться двічі через getpass тільки в терміналі. Якщо приховане
введення неможливе, CLI відмовляється, а не переходить до echo/stdin.
Немає параметра --password чи читання пароля з environment.

```bash
cd /home/ugvs2/ugv_optical_navigation
export UGV_HMI_STATE_DIR=/home/ugvs2/.local/state/ugv-hmi
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli bootstrap-admin YOUR_ADMIN_LOGIN
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli create YOUR_OPERATOR_LOGIN operator
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli list
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli role YOUR_OPERATOR_LOGIN admin
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli password YOUR_OPERATOR_LOGIN
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli revoke YOUR_OPERATOR_LOGIN
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli disable YOUR_OPERATOR_LOGIN
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli enable YOUR_OPERATOR_LOGIN
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli prune
```

YOUR_* — обрані персональні логіни, не готові стандартні акаунти.
Останнього активного admin не можна деактивувати чи демотувати. Перевірка
і зміна виконуються в BEGIN IMMEDIATE, включно з паралельними CLI-запусками.
CLI revoke дозволений навіть для останнього admin: пароль не змінюється,
доступ можна відновити звичайним входом. Акаунти не видаляються фізично.

## Production WSGI і єдиний serial-читач

Немає module-global app=create_app(). Звичайний імпорт app.py або
wsgi.create_wsgi_app() НЕ запускає телеметрію. Gunicorn має використовувати
обидва файли: wsgi.py factory та gunicorn_conf.py. post_worker_init
одного worker викликає runtime.start_telemetry при UGV_HMI_TELEMETRY=1.
Тільки тоді читач одержує opener=open_with_owner_helper і стартує.

Конфігурація: 127.0.0.1:8082, workers=1, gthread threads=4, preload=false,
reload=false, max_requests=0. Hook відмовляється при кількох workers,
preload або reload. Окремий flock serial-reader.lock у приватному state
забороняє другий читач із цим state; його FD тримається весь час роботи
читача. Під час shutdown спершу закривається читач. Якщо thread ще живий,
lock не відпускається до виходу процесу. Блокування owner helper,
O_RDONLY/flock/TIOCEXCL, перевірки до/після відкриття та звірка ідентичності
не змінені; pixhawk_telemetry.py, pixhawk_owner_client.py і root helper
ідентичні вихідній версії. У simulator.py додано лише hooks обліку місій;
фізичного керування та нових переходів DEMO немає. Немає fallback на небезпечний backend.

Не робити HUP/USR2, rolling restart, --reload, --preload, кілька instances
чи інший state для другого читача. Для оновлення — погоджена повна
зупинка старого процесу, перевірка власників, потім один новий процес.
Новий lock не замінює kernel/owner перевірки старого v0.3, який цього lock
ще не використовує. Для спільного serial потрібен окремий MAVLink router.

Файл ugv-hmi.service.example лише шаблон. Порівняти з чинною unit перед
будь-якою заміною. User/Group=ugvs2, SupplementaryGroups=dialout, UMask=0077.
Не вмикати NoNewPrivileges/PrivateUsers/обмежений /proc, що зламає вже
схвалений вузький sudo helper. Не розширювати його sudoers-правило.
Gunicorn/NGINX тут не встановлені: справжній WSGI/TLS запуск не тестувався.
Hooks перевірені тестами з fake worker, без обладнання і мережі.

## HTTPS через reverse proxy — план, НЕ застосовано

1. Перевірити, що 192.168.110.59 — чинна Wi-Fi адреса ровера. Отримати
   сертифікат з IP SAN 192.168.110.59 від приватного CA, довіреного планшету.
   CA private key зберігати поза ровером і репозиторієм; серверний private
   key — root:root 0600 у /etc/ugv-hmi/tls/server.key. Не обходити перевірку TLS.
2. Nginx слухає тільки 192.168.110.59:443. Не додавати listener 80 або
   прямий Wi-Fi доступ до 8082. Наявний WireGuard і його маршрути не змінювати.
   Firewall/Wi-Fi доступ — окреме погодження, лише потрібні клієнти/підмережа.
3. PUBLIC_ORIGIN=https://192.168.110.59, UGV_HMI_TRUST_PROXY=1. Backend
   доступний тільки loopback. Nginx ЗАМІНЮЄ X-Forwarded-For і Proto, прибирає
   Forwarded/X-Forwarded-Host; ProxyFix довіряє рівно одному proxy для IP/proto,
   не довіряє forwarded host. Прямий доступ до backend — лише довіреним
   локальним OS-процесам. Не ставити ProxyFix на публічний listener.
4. Увесь /, включно зі static, проксувати у Flask; direct static alias може
   обійти захист JS. Login CSS свідомо публічний. Proxy/cache вимкнений.
   Access log має $uri без query, cookie, Authorization чи body. Перевірити
   distro logrotate. Secure cookies обов'язкові на HTTPS; не вимикати для Wi-Fi.
5. Перед відкриттям планшету: перевірити довіру CA, login/logout, обидві ролі,
   API 401/403, CSRF, idle/max/revoke, відсутність секретів у всіх OS/web logs.
   Лише після окремого погодження перевіряти telemetry і DEMO на живому HMI.

Шаблони: auth.env.example (лише несекретні налаштування, копія поза repo
0600), nginx-ugv-hmi.conf.example, ugv-hmi.service.example. Вони нічого
не встановлюють автоматично. HTTP дозволено лише explicit loopback dev:

```bash
# ПРИКЛАД майбутнього локального запуску без телеметрії; зараз НЕ виконано:
/home/ugvs2/.venv/bin/python -m web_hmi.app --http-loopback --port 8082
```

## Тести

З кореня окремої копії, без серверів, root, sudo, справжнього /proc scan чи tty:

```bash
cd /tmp/ugv-hmi-auth-review
PYTHONDONTWRITEBYTECODE=1 /home/ugvs2/.venv-ugv-hmi-auth/bin/python -m unittest \
  web_hmi.test_hmi web_hmi.test_pixhawk_telemetry \
  web_hmi.test_pixhawk_owner_helper web_hmi.test_auth web_hmi.test_mission_statistics web_hmi.test_periods_v2 web_hmi.test_password_policy -v
```

Результат — test-results.txt. Наявні тести адаптовані через справжній login
і CSRF; немає AUTH_DISABLED або тестового обходу в production. Клієнти для
конкурентних DEMO-тестів входять до запуску конкурентних команд, щоб
окремо перевіряти state machine, не навантажуючи паралельний login limiter.
UI рендериться Flask test_client; JS у справжньому браузері, tablet/TLS,
Gunicorn і системна unit потребують окремої перевірки перед rollout.

## Встановлення після перегляду — жодна команда нижче зараз не виконана

Не застосовувати артефакти автоматично до live release. Спершу порівняти
source_manifest.json з чинними файлами: якщо вони змінилися — перенести
diff вручну в нову review-копію, повторити тести. Базовий owner helper уже
інтегрований у вихідному app.py; старий privileged/hmi_integration.patch
повторно НЕ застосовувати.

Підготовка від ugvs2, без зупинки служби:

```bash
set -eu
cd /home/ugvs2/ugv_optical_navigation
# ARCHIVE — збережений після перегляду шлях, не завантаження з мережі.
# CANDIDATE — точний новий постійний каталог із FINAL_LOCATION.txt.
CANDIDATE=/home/ugvs2/ugv_optical_navigation_candidates/hmi-auth-password8-20261113-140200-61f497
ARCHIVE="$CANDIDATE/ugv-hmi-password8-candidate.tar.gz"
RELEASE=/home/ugvs2/ugv-hmi-auth-release
# Не перезаписувати наявний release.
mkdir -m 0700 "$RELEASE"
tar -xzf "$ARCHIVE" -C "$RELEASE"
cd "$RELEASE"
PYTHONDONTWRITEBYTECODE=1 /home/ugvs2/.venv-ugv-hmi-auth/bin/python -m unittest \
  web_hmi.test_hmi web_hmi.test_pixhawk_telemetry \
  web_hmi.test_pixhawk_owner_helper web_hmi.test_auth web_hmi.test_mission_statistics web_hmi.test_periods_v2 web_hmi.test_password_policy -v

# У state лише приватні файли, поза будь-яким release/repo.
install -d -m 0700 /home/ugvs2/.local/state/ugv-hmi
install -d -m 0700 /home/ugvs2/.config/ugv-hmi
# Не перезаписувати існуючий env без backup/перегляду.
test ! -e /home/ugvs2/.config/ugv-hmi/auth.env
install -m 0600 web_hmi/deploy/auth.env.example /home/ugvs2/.config/ugv-hmi/auth.env
/home/ugvs2/.venv/bin/python -m web_hmi.admin_cli bootstrap-admin YOUR_ADMIN_LOGIN
```

Якщо залежностей бракує, зупинитися. У цьому сеансі pip/apt НЕ виконувалися.
Для майбутнього production бажано окремий venv та reviewed lockfile з
перевірених wheels; requirements_production.txt задає межі, не є lockfile.
Не змінювати спільний робочий venv без його окремого backup і погодження.
Gunicorn/Flask/Werkzeug мають пройти інтеграційний smoke test у вибраній
версії. Наявна тестова версія Flask 3.1.1, без Gunicorn/NGINX.
Для календарних вікон потрібна системна IANA tzdata з Europe/Kyiv;
у тестовому середовищі вона є. Жодних пакетів тут не встановлювалося.

Системну частину робить адміністратор ПІСЛЯ окремого погодження переключення.
Спочатку зберегти baseline unit і конфігурації (каталог root-only):

```bash
sudo install -d -o root -g root -m 0700 /var/backups/ugv-hmi-auth
# Якщо такі backup уже є — зупинитися, не перезаписувати попередні.
sudo test ! -e /var/backups/ugv-hmi-auth/unit-before.txt
sudo sh -c 'umask 077; systemctl cat ugv-hmi > /var/backups/ugv-hmi-auth/unit-before.txt'
# Окремо скопіювати реальні unit FragmentPath і DropInPaths із systemctl show,
# а не використовувати concatenated systemctl cat як готовий unit для відкату.
systemctl show ugv-hmi -p FragmentPath -p DropInPaths
```

Переглянути шаблон service; для окремого release змінити WorkingDirectory
на /home/ugvs2/ugv-hmi-auth-release, ExecStart — на погоджений production
venv. Зберегти реальні змінювані unit/drop-in файли через sudo cp -a до
/var/backups/ugv-hmi-auth зі збереженням імен. Nginx config/TLS також
резервуються до зміни; якщо файлів раніше не було — зафіксувати це для
відкату. Наявні root helper і sudoers НЕ змінювати.

Порядок перемикання (це вже запускає читач, зараз ЗАБОРОНЕНО виконувати):
повністю stop ugv-hmi → перевірка завершення старого PID і звільнення 8082 →
sudo -n fixed owner helper (тільки метадані) → якщо complete і owners=[] →
встановлення переглянутого unit → systemd-analyze verify → daemon-reload →
start рівно одного worker. Потім локальні auth-smoke tests. Лише з валідним
TLS та погодженим Wi-Fi доступом встановити nginx конфігурацію, nginx -t
та окремо погоджений reload. Ні enable, ні restart, ні reload тут не виконано.

## Відкат

Оскільки rollout не виконано, зараз відкат робочої служби не потрібен.
Для цього review досить зберегти/видалити тільки окрему копію; не чіпати live.
Після майбутнього rollout:

1. Погоджено закрити новий Wi-Fi listener/повернути попередній nginx config,
   виконати nginx -t і погоджений reload. Не залишати стару HMI без auth
   доступною з Wi-Fi. WireGuard не змінювати.
2. Повністю зупинити нову ugv-hmi, дочекатися завершення reader/process і
   звільнення порту. Не запускати старий reader паралельно.
3. Повернути з backup РЕАЛЬНІ unit/drop-in файли та попередній ExecStart /
   WorkingDirectory. Виконати systemd-analyze verify, daemon-reload і лише
   погоджений start попереднього release на loopback.
4. Auth SQLite/key/state 0700/0600 залишити приватними для розслідування,
   не копіювати до repo/static, не видаляти при відкаті. Для повернення
   саме стану auth використовувати узгоджений offline backup DB+key,
   врахувати повернення старих sessions/passwords; після restore відкликати
   сесії та перевірити останнього admin. Не відкочувати БД без потреби.
5. Не видаляти/перевстановлювати owner helper чи його вузький sudoers:
   вони в цьому rollout не змінюються. Перевірити лише одного serial-власника.

Новий release встановлюється поряд зі старим, тому джерела попередньої
HMI залишаються готовими для повернення. Схема v2 змінює значення часових агрегатів. Попередній статистичний v1
кандидат ВІДМОВИТЬСЯ від version=2: не змінювати версію вручну і не запускати
v1 поверх v2. Для повернення v1 потрібен окремо погоджений offline restore
узгодженої v1 БД+key з урахуванням втрачених змін акаунтів/сесій/місій.
Auth-only кандидат без статистики ігнорує нові таблиці, але не веде облік.
Докладно див. README_PERIODS_V2.md. Навігація і реальне керування не змінені.

## Джерела для security/deployment-рішень

- Flask security / cookie / CSRF / TRUSTED_HOSTS:
  https://flask.palletsprojects.com/en/stable/web-security/
- Werkzeug password hashing:
  https://werkzeug.palletsprojects.com/en/stable/utils/#werkzeug.security.generate_password_hash
- Gunicorn settings та hooks:
  https://gunicorn.org/reference/settings/
- Nginx proxy headers:
  https://nginx.org/en/docs/http/ngx_http_proxy_module.html
