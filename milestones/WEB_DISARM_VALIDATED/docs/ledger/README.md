# HM2131L/ArUco через єдиного owner — підготовлений реальний код

## Фізична перевірка 17.11.2026

HM2131L працює у1280x720 з незміненим fisheye calibration і marker map v2.
Після повідомлення про ID23 камера насправді розпізнала ID24 у184/184 кадрах;
ID23 не знайдено. Новий transport-free camera worker окремо перевірений на
реальному зображенні:128/128 скінченних pose callbacks, marker24. Це не
симуляція. Serial opens0, ODOMETRY sent0. Оператор підтвердив: маркер лише
поставлений перед камерою або кріплення змінене. Тому map-frame координати
не вважаються фізично валідними й у EKF не передаються.

Неможливо одночасно зберегти старі calibration/map, використати довільно
переміщений маркер/камеру та чесно стверджувати коректність координат карти.
Потрібне відновлення фізичної установки або окрема перевірена перекалібровка;
останню не виконували. Пропадання PreArm поки НЕ підтверджене: валідний
ExternalNav ще не передавався.

## Мінімальна архітектура

- optical/owner_optical_worker.py — рефакторинг існуючого shadow: те саме
  fisheye remap, DICT_5X5_50, refine/solvePnP, selector V8, mapv2,
  camera_to_rover_pose, FLU→FRD. Оригінальні calibration/config/map скопійовано
  побайтово без змін. Worker не імпортує pymavlink, не відкриває serial і
  не надсилає команд message interval чи EKF source switch.
- Окремий camera thread у чинному owner передає тільки latest measured pose
  в пам'яті. Немає додаткового GPIO/serial власника, RPC writer або черги поз.
- Owner serial thread спочатку виконує наявний DISARM tick, далі опційні
  read-only діагностичні запити й ODOMETRY. При активному/несправному DISARM
  command path NAV/diagnostic writes пропускаються. Код DISARM engine не змінено.
- Оптичний stamp — SensorTimestamp(CLOCK_BOOTTIME), консервативно переведений
  у monotonic; missing/future/старше0.2с відхиляється. Старий кадр повторно
  не відправляється. Маркер зник — pending pose анулюється. FIFO/autoretry немає.
- 10Hz максимум; serial tick20ms у ввімкненому optical профілі. ODOMETRY
  source1/191, LOCAL_FRD/BODY_FRD, planar z0 і covariance/quality з попереднього
  shadow збережені як старий planar контракт. Нових значень координат для
  маскування відсутності камери немає. TX fault зупиняє NAV без retry, але
  не завершує owner/HMI/DISARM.
- Default/перший install: --externalnav-observe-only, нуль ODOMETRY writes.
  --externalnav-tx у коді додатково вимагає root-owned, asset-hash/boot-bound
  alignment-verification record. Він НЕ створюється цим installer, оскільки
  оператор явно повідомив про невідповідність фізичної установки.

SensorTimestamp clock reference:
https://docs.libcamera.org/master/internal-api/namespacelibcamera_1_1controls.html

## Практичне встановлення observe-only та отримання актуальних RC-параметрів

Sudo агента зараз потребує пароля. Підготовлена команда для звичайного
термінала оператора на Pi. Перед нею: ELRS DISARM, neutral throttle, ніяких
перемикань ARM під час restart. DISARM короткочасно недоступний між stop/start
owner і gateway; фізичний E-STOP та ELRS лишаються незалежними.

```bash
cd /home/ugvs2/ugv_optical_navigation/externalnav_recovery_prepared
sha256sum -c SHA256SUMS
sudo /usr/bin/python3 -I "$PWD/install_observe.py"
```

Installer вимагає свіжий DISARMED та idle DISARM ledger (історія може бути
непорожньою), перевіряє hashes,
ставить root-owned0755/0644 candidate в /opt/ugv-externalnav-recovery/20261117.
Батьківський каталог також явно0755 — не залежить від umask. Backup фактичних
units і SQLite API snapshot DISARM DB — приватно у /var/backups/ugv-disarm.
Auth DB/key не копіюються/не мігруються, HMI не перезапускається, runtime
DISARM flags/inhibit не змінюються. Старий owner зупиняється, перевіряється
PID0/owners=[], тільки тоді прибирається доведений stale backend socket і
стартує новий owner. Gateway стартує з чинними налаштуваннями. Код старого
/opt/ugv-disarm package не перезаписується. Помилка startup повертає старий
owner profile без втрати актуальних state/ledger. Повторно installer не запускати.

Очікується OWNER_OPTICAL_OBSERVE_RUNNING; це НЕ означає VISION healthy.
Перевірити endpoints (після20с для завершення однопрохідних param queries):

```bash
curl --noproxy '*' --max-time 3 -sS http://127.0.0.1:8084/api/externalnav
curl --noproxy '*' --max-time 3 -sS http://127.0.0.1:8084/api/owner-diagnostics
curl --noproxy '*' --max-time 3 -sS http://127.0.0.1:8082/api/telemetry
systemctl is-active ugv-hmi.service ugv-command-gateway.service ugv-hmi-auth.service
```

/api/externalnav: tx_enabled=false, sent=0, alignment_verified=false очікувані.
finite_pose_count означає числову придатність вимірювання, не підтверджені
координати карти. camera_thread_alive=false/CAMERA_ERROR означає camera problem,
а не permission на передавання останньої пози. Не відкривати паралельний MJPEG
stream, який створює rpicam-vid і займає ту саму камеру. camera_stream.service
автоматично не зупиняється й не змінюється.

## ELRS, RC override та актуальні параметри

Новий diagnostics cache зберігає STATUSTEXT, RC_CHANNELS, SYS_STATUS і лише
allowlisted PARAM_VALUE. За --diagnostic-params сам serial-owner один раз
запитує RC5_OPTION, RCMAP_THROTTLE, RC_OPTIONS, RC_OVERRIDE_TIME, RC_PROTOCOLS,
ARMING_CHECK/REQUIRE/SKIPCHK, VISO_TYPE та SERIAL1..8_PROTOCOL. Не частіше1Hz,
тільки за fresh DISARMED, без retry. PARAM_REQUEST_READ не змінює параметри;
PARAM_SET/RC override/ARM/mode/EKF commands у цьому коді відсутні.
Непідтримуваний параметр лишається невідомим, а не підміняється старим значенням.

Результат покаже фактичний RC5_OPTION, policy прийняття override та можливий
UART RCIN. Це дозволить звузити receiver path без зміни конфігурації.
RC_CHANNELS зі свіжими часовими мітками самі по собі не доводять, що всі
дані прийшли з RF; отримані MAVLink RC overrides можуть перекривати RC receiver.
RC_OPTIONS/RC_OVERRIDE_TIME — policy, не доказ поточного active override.
Кеш echoed_override_messages дає позитивне свідчення, якщо таке повідомлення
переслане в цей serial-потік. Його відсутність НЕ доводить відсутність override
на іншому інтерфейсі Pixhawk. Встановлений Pi owner сам RC override не надсилає.
Постійний LED приймача не доводить доставку зміни CH5 у автопілот.

Primary RC input reference:
https://ardupilot.org/dev/docs/mavlink-rcinput.html

CH5 на передавачі змінюється, але раніше Pixhawk давав999; точний винний вузол
ще не встановлений. Наступні факти — current RC params і синхронне перемикання
у live monitor, а не зміна channel mapping навмання. При RX UART configured
RCIN і відсутності override-пояснення звіряють активну модель/RF output,
receiver protocol/output та фізичний UART. Параметри скриптом не змінюються.

## Перед реальним ExternalNav TX

Цей install не передає пози і не прибирає PreArm. Перед TX необхідно:
1. Правильний фізичний маркер і його положення/orientation за mapv2.
   Камера/кріплення відповідають calibration/extrinsics; зараз це НЕ виконано.
2. Стабільна поза і очікувані напрямки/координати в observe-only на місці.
3. ELRS DISARM/neutral та незалежний E-STOP; оператор має розуміти, що
   відновлення VISION може прибрати PreArm gate, а ARMED тумблер не можна
   залишати в ARM під час цієї зміни.
4. Окреме створення truthful alignment verification й контрольоване перемикання
   observe-only→TX; у цьому пакеті така команда свідомо не видається для
   підтверджено неправильної фізичної установки. Не обходити guard.

## Виконані вузькі перевірки

Syntax всіх candidate modules. In-memory wire encoding: ODOMETRY source1/191,
observe-only0writes, alignment guard0writes, DISARM priority, stale/duplicate
frame rejection. Read-only diagnostic wire message саме PARAM_REQUEST_READ,
першийRC5_OPTION, targets1/1; ARMED не дає diagnostic query. Фізичних FD у тесті0.
На реальній камері transport-free worker128frames/128finitecallbacks/marker24,
без serial і без send. Старі HMI/DISARM code paths не змінені. Повний unit-цикл,
загальні аудити, EKF/параметри, ARM і привід не запускалися.

Агент не виконував install: sudo потребує операторського пароля. Поточні
робочі HMI/DISARM залишені на попередньому owner PID80074.


## Адресне виправлення nonempty ledger, 17.11.2026

Див. LEDGER_FIX.md. Empty-table requirement прибрано. Read-only перевірка
state/reason/evidence повертає LEDGER_IDLE для двох завершених SENT/FULL_WRITE.
Pending/unreviewed contexts блокуються. Після stop gateway — повторна перевірка
до stop owner. Завершені SENT не втрачають delivery state/reason при restart;
спостереження після зміни owner epoch залишається UNKNOWN. UUID не replay-яться.
Повний logical dump ledger перевіряється до/після startup; DB/audit не підміняються.
Fallback profile зберігає вузьке виправлення terminal-history у engine.
