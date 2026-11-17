**Web DISARM — успішний тест на нерухомому ровері, 17.11.2026**

Оператор підтвердив правильну роботу. Журнал незалежно зафіксував свіжий перехід ARMED → DISARMED без проміжного UNKNOWN. Аналіз лише на читання; служби, параметри та керування не змінювалися. Створено тільки ці звітні артефакти.

| Подія | Час за Києвом | Доказ |
|---|---|---|
| ARMED спостерігається | 19:21:13.961490 | raw.jsonl, рядок 430, FRESH HEARTBEAT |
| HMI прийняв HTTP-запит для audit | 19:21:16.702931 | auth audit id144; це час входу запиту, не завершення його обробки |
| Повний запис DISARM у serial | ≈19:21:16.773804 | ledger FULL_WRITE, sent_at=1762.718313144, serial188 |
| Owner зафіксував свіжий DISARMED після відправлення | ≈19:21:16.911753 | terminal ledger updated=1762.856262327 |
| Logger спостерігає DISARMED | 19:21:17.253333 | raw.jsonl, рядок445, FRESH HEARTBEAT |
| HMI audit DISARMED_OBSERVED | 19:21:17.370385 | auth audit id146, SENT/FRESH_POST_SEND_HEARTBEAT |

Часи serial/owner перераховані з monotonic за найближчою парою UTC/monotonic logger; вони орієнтовні. Затримки owner нижче обчислені безпосередньо з monotonic.

Один новий запит `b6853e32-6237-45d0-9551-084748b41687`, actor `mykyta`, target1/1, owner_epoch `d47c6f6a-a2b9-43d1-bba9-6a97a082694a`. `SENT/FULL_WRITE` підтверджує повний запис пакета в serial. Чинний sender кодує COMMAND_LONG, MAV_CMD_COMPONENT_ARM_DISARM=400, param1=0, param2=0. Окремого packet capture журнал не містить. Приймання конкретного пакета Pixhawk лише цим записом не доводиться.

| Інтервал | Час |
|---|---:|
| Прийняття запиту owner → FULL_WRITE | 39,819 мс |
| Прийняття запиту owner → запис owner про свіжий DISARMED | 177,768 мс |
| FULL_WRITE → запис owner про свіжий DISARMED | 137,949 мс |
| Прийняття запиту owner → перше спостереження DISARMED logger | 519,370 мс |
| Вхід HTTP-запиту в HMI audit → HMI audit DISARMED_OBSERVED | ≈667,454 мс (wall clock) |

Точний інтервал «фізичне натискання кнопки → DISARMED/відображення в браузері» НЕ ВИМІРЯНО: часової мітки кліку й відмальовування UI немає. Наведені інтервали мають серверні точки відліку та включають затримки HEARTBEAT/опитування у відповідних місцях.

**ACK:** не записується цим пасивним logger. Header: NOT_EXPOSED_BY_FIXED_ENDPOINTS_NO_CONTROL_STATUS_RPC_REQUESTED. SQLite також не зберігає ACK. Тому ACK/result невідомі, а не «ACK відсутній». Доступ до status RPC для витягування volatile uncorrelated ACK не виконувався: він може оновлювати ledger. Навіть такий ACK не є доказом виконання саме цього UUID.

Додатковий факт: STATUSTEXT `Throttle disarmed` отримано на monotonic1762.747527053, через28,214 мс після FULL_WRITE. Термінальна причина ledger — DISARMED_STATE_OBSERVED_NOT_COMMAND_PROOF. Чинна логіка вимагає свіжий HEARTBEAT від target після sent_at із більшим rx serial. Автоповтору цього запиту в записі немає.

Висновок: **оператор підтвердив успішний функціональний тест; журнал підтвердив повний serial write і наступний свіжий ARMED → DISARMED.** ACK і точний click-to-DISARMED залишаються невиміряними. Цей нерухомий тест не вимірює гальмівний шлях чи час зупинки коліс у русі; RPM у logger недоступні.

Джерела: `raw.jsonl` (рядки430,444,445), `STATIONARY_TEST_ANALYSIS.json`, read-only auth audit144–146. Logger ще дописує raw до запланованого завершення; аналіз прив'язаний до завершеного префікса файла, його bytes/lines/SHA256 наведені в JSON. Звичайні summary.json/SHA256SUMS logger створить сам після завершення. Наявний процес logger не зупинявся.
