# RC8 і PWM виходів 5/6 через штатний serial-owner

У штатному owner декодовані MAVLink-кадри передаються в command observer до фільтра типів. Чинний diagnostics читає RC_CHANNELS, але не зберігає SERVO_OUTPUT_RAW. За наявними GET неможливо визначити, чи Pixhawk уже надсилає повідомлення 36. Патч додає обробку в цю точку без зміни serial reader або доступу до FD з HTTP. Приймає тільки джерело 1/1 та bank `port=0`, який Rover 4.7.2 використовує для SERVO5/6.

## Майбутнє контрольоване переключення

1. Після погодження підготувати root-owned копію чинного дерева `/opt/ugv-tilted-observe/20261117/package` у `/opt/ugv-brake-pwm-observe/package` і накласти `brake-pwm-telemetry.patch`. Патч змінює лише `owner/stop_latch_test/diagnostics.py` і `owner/stop_latch_test/hold_runner.py`.
2. Зберегти ефективний unit і чинний `zzz-externalnav-observe-recovery.conf`. Додати окремий runtime drop-in `/run/systemd/system/ugv-hmi.service.d/zzzz-brake-pwm.conf`, який змінює WorkingDirectory і всі три компоненти PYTHONPATH на нове дерево, скидає ExecStart порожнім рядком `ExecStart=` та повторює чинний ExecStart із `--externalnav-observe-only --observe-brake-pwm`. Зберегти journal, ledger, socket, inhibit, UID і GID. Після daemon-reload звірити ефективні WorkingDirectory, PYTHONPATH і ExecStart.
3. У погодженому вікні зупинити лише `ugv-hmi.service`. Підтвердити MainPID=0, відсутність власника через привілейований helper і відсутність активних з’єднань gateway/backend через ss. Якщо backend.sock лишився, звірити його тип, inode, UID/GID і mode, перевірити відсутність слухача та отримати ECONNREFUSED локальним підключенням без даних. Перевірити inode повторно перед видаленням лише цього сокета. Зробити один запуск і перевірити одного власника serial, свіжий DISARMED, ExternalNav `tx_enabled=false` і `sent=0`, HMI, gateway, inhibit та STOP_LATCH.
4. Пасивно подивитися `brake_pwm` у GET. Якщо bank 0 не надходить, для окремо погодженого запуску замінити прапор пасивного режиму на `--brake-pwm-interval-us 500000`. Він знову спочатку слухає п'ять секунд за свіжого DISARMED. За відсутності потрібних кадрів надсилає один MAV_CMD_SET_MESSAGE_INTERVAL для ID 36, 500000 мкс. Під час активної DISARM транзакції запит відкладається. ACK відображає результат запиту інтервалу, а дані PWM стають доступними лише після отримання кадрів.
5. Оператор тримає CH8 послідовно в APPLY та RELEASE, знімаючи кілька стабільних свіжих зрізів. RC і servo поля мають окремі часи отримання та вік. За значень `null`, віку від трьох секунд або суперечливих вимірів не переносити значення в Lua. Нуль у SERVO_OUTPUT_RAW — сирий показник без корисного PWM. Це телеметрія виходу, яку повідомляє Pixhawk, без незалежного вимірювання електричного сигналу.

```sh
curl -fsS http://127.0.0.1:8084/api/owner-diagnostics | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["brake_pwm"], indent=2))'
```

## Rollback

Якщо застосовано лише пасивний прапор, параметр інтервалу у Pixhawk не чіпався. Якщо відправлено активний запит, повернення Pi owner саме по собі може не прибрати runtime інтервал у Pixhawk. Погоджений режим `--brake-pwm-interval-us 0` повертає штатний інтервал, а `-1` вимикає потік. Використовувати тільки той варіант, який відповідає відомому вихідному стану. Значення 0 не відновлює довільний попередній ручний інтервал.

Зупинити лише діагностичного owner, підтвердити відсутність власника, прибрати лише `zzzz-brake-pwm.conf`, перевірити stale socket і за потреби прибрати його за кроком 3. Після daemon-reload перевірити ефективний `--externalnav-observe-only` із чинного recovery drop-in і лише за погодженим rollback запустити штатний owner. Перевірити одного власника, свіжий HEARTBEAT, HTTPS HMI, gateway, ExternalNav, inhibit та digest ledger. Штатний реліз і бази лишаються на вихідних шляхах.

Офлайн перевірено накладання патча та Python-компіляцію, фільтри джерела і bank, RC8, timestamps, fresh/stale, відсутність записів у пасивному режимі, заборону профілю ExternalNav TX та один декодований у пам’яті пакет 511 із частотою 2 Гц. Нічого не встановлено й не надіслано Pixhawk.

## Одноразове зчитування десяти параметрів виходів

`brake-servo10-readonly.patch` накладається на копію чинного `/opt/ugv-brake-pwm-observe/package`. Після окремого погодження встановити копію у root-owned дерево `/opt/ugv-brake-servo10/package`. Зберегти ефективний unit, чинний `zzzz-brake-pwm.conf`, контрольну суму ledger та ідентичність backend-сокета. Новий runtime drop-in `zzzzz-brake-servo10.conf` замінює WorkingDirectory і три шляхи PYTHONPATH на нове дерево та додає до чинного ExecStart лише `--read-brake-servo-params`. Залишити `--externalnav-observe-only --observe-brake-pwm` і чинний inhibit.

Виконати контрольований stop/start і перевірку залишкового сокета за процедурою вище. Профіль робить по одному іменному запиту для FUNCTION, MIN, MAX, TRIM і REVERSED на виходах 5/6, за свіжого DISARMED і без активної команди. У чинному GET зберегти `parameters` та `diagnostic_requests_attempted`. Приймати результат лише за наявності всіх десяти відповідей. Повторний запуск профілю повторить серію читань.

Для rollback зупинити owner, прибрати лише `zzzzz-brake-servo10.conf`, виконати daemon-reload, звірити вихідний профіль пасивного PWM та застосувати перевірку залишкового сокета перед одним запуском. Перевірити один serial-owner, свіжий DISARMED, TX=false, sent=0, inhibit і STOP_LATCH.
