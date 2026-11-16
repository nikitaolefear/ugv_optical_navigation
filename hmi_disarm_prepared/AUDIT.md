# Фінальний аудит Web DISARM — 2026-11-16

> Історія попереднього strict-ACK профілю. Поточний DISARMED_OBSERVED
> контракт і перевірки описані в README.md та OBSERVED_VALIDATION.md.
> Наведений тут ліміт 254 не є залежністю нового runtime.

Гілка `feature/ugv-hmi-disarm`, HEAD до коміту
`f4462926b40dc888a77b18e6fbce105dd0a776a5`.
Результат: ізольовані перевірки пройдені; production інтеграція НЕ виконана.
Умови допуску нижче не слід замінювати формулюванням «готово до production».

## Виявлені та виправлені дефекти

1. **Клієнт не перевіряв Unix-сервер.** Додані private parent, mode/uid/type
   socket, перевірка peer UID на клієнті та незмінності inode під час connect.
   Symlink та socket із group/other правами відхиляються до надсилання.
2. **Відповідь не була прив'язана до конкретного IPC-виклику.** Доданий
   випадковий nonce разом з op/request_id і перевіркою їх точного збігу.
   Старі або переплутані відповіді відхиляються; результат невизначений,
   автоматичного повторного DISARM немає.
3. **Повільний клієнт міг поновлювати timeout кожним байтом.** receive()
   тепер має загальний monotonic deadline, розмір обмежений 4096 байт.
   Duplicate JSON fields та NaN/Infinity також відхиляються.
4. **Старий ACK команди 400 міг підтвердити наступну транзакцію.** До
   виправлення всі запити мали source 255/190, а unaddressed ACK допускався.
   Тепер кожна транзакція має одноразовий component ID у durable identities.
   Перевіряються точний autopilot source, command=400 та точні ACK target
   system/component. Старі ACK після помилки або рестарту й ACK без адреси
   ігноруються. Для успіху додатково потрібен HEARTBEAT після ACK із
   DISARMED, новим counter/stamp і свіжістю. Новий HEARTBEAT без ACK не є успіхом.
5. **Нова БД автоматично надавала DISARM admin/superadmin.** Прибрано
   з усіх DEFAULT_ROLES. Тести надають право явно лише у приватних
   тимчасових БД; перевірені default deny та відкликання права.
6. **Збій запуску IPC-thread залишав socket.** Додане закриття та
   видалення тільки власного socket при невдалому start, без shutdown-deadlock.

## Перевірені інваріанти

- DISARM encoder пакує лише COMMAND_LONG command=400, param1=0, param2=0,
  param3..7=0, confirmation=0. Він не відкриває transport. Serial fd надходить
  тільки з tick чинного власника, а не з HTTP/IPC payload.
- HMI runtime забороняє локальний serial reader при увімкненому DISARM.
  Код працюючого власника та HMI не змінений; створено лише prepared-копії.
- UUID intent є durable/idempotent. Конкурентна транзакція відхиляється.
  Втрата відповіді, partial write, write exception або рестарт не спричиняють retry.
- Негативний ACK, stale/link loss, зміна source, clock fault та timeout
  не породжують CONFIRMED. Після рестарту незавершене отримує ERROR_RESTART.
- Наявний HoldBench cancellation/interlock утримує STOP_LATCH, не
  викликає RESET і не дозволяє активувати HOLD-тест під час DISARM.
- Permission + session + CSRF + Origin перевіряються до доставки. Audit
  intent зберігається до dispatch; його відмова блокує доставку.
- Подвійний клік не створює другий POST; UI не називає send/ACK успіхом.
  При втраті каналу/тайм-ауті чітко показує невизначений результат.

## Обмеження, що потребують погодження до інтеграції

**COMMAND_ACK не має UUID транзакції.** Addressed ACK fields є MAVLink2
extensions: [офіційне визначення](https://mavlink.io/en/messages/common.html#COMMAND_ACK).
Використана консервативна схема без повторного використання компонентів:
source system=255, component=1..255 крім HOLD component=190. Це не криптографія.

- Потрібні перевірка підтримки addressed ACK конкретною прошивкою/каналом
  та резервування цієї множини source ID для engine. Інші GCS не повинні
  використовувати її для command 400. Немає fallback до unaddressed ACK.
- Ліміт **254 нових транзакції на identity DB** явний. Після нього відмова
  без write. Рестарт не скидає ліміт. Для довготривалої production роботи
  потрібне окреме рішення про адресний простір/безпечне обслуговування.
- Backup/rollback не повинен повертати identities назад. Видалення DB
  або відновлення старої копії порушує гарантію щодо пізніх ACK.
- Shared UID `ugvs2` залишається довіреною межею: інший скомпрометований
  процес того самого UID/root може обійти Web-авторизацію чи підмінити сервер.
  Peer UID і nonce не захищають від такого локального компрометування.
- Реальний DISARM, фізичний Sigma A1035, тяга/VESC і апаратний час реакції
  не перевірялися. SQLite/fsync і Python не дають hard-real-time гарантій.
  Незалежний встановлений фізичний E-STOP залишається окремим захистом.

## Результати повторних перевірок

| Набір | Пройшло |
| --- | ---: |
| DISARM engine / MAVLink packets | 29 |
| Unix IPC | 14 |
| Підготовлений owner / HoldBench | 6 |
| HMI rights / CSRF / audit / errors | 18 |
| Flask → Unix IPC → fake serial → confirmation | 1 |
| Регресії auth / access / DEMO | 34 + 22 + 18 |
| Незмінене production STOP | 61 |
| **Разом Python** | **203/203** |
| Chromium 1280×800 та 800×1280 | **14/14** |

У тестах тільки тимчасові БД/Unix-сокети й fake serial/MAVLink. Код
чинних служб, production permissions, GPIO, VESC, E-STOP та Pixhawk не змінені.
Read-only systemd перевірка показала active/running, ті самі PID 960 і 1524.
Синтаксис, whitespace та source manifest перевіряються окремо.

Точний перелік майбутнього коміту — `COMMIT_FILES.txt`; backup/config/rollback —
`DEPLOYMENT.md`. Stage/commit/push не виконувалися. Зупинка перед розгортанням.
