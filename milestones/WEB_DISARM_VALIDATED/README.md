# WEB_DISARM_VALIDATED — 17.11.2026

Прийнята оператором контрольна точка після двох успішних фізичних випробувань Web DISARM: на нерухомому ровері та у контрольованому русі через ELRS. Це знімок фактично встановленої версії, а не чергова підготовлена збірка. Системні файли, процеси, параметри Pixhawk, EKF, ARMING_CHECK і навігація під час збереження не змінювалися; випробування не повторювалися.

## Фактичний склад

- `installed/frontend_gateway/` — побайтова копія `/opt/ugv-disarm/package`: чинні authenticated Web HMI та gateway. Вкладений base-owner збережений як частина встановленого пакета, але не є поточним owner.
- `installed/owner_externalnav/` — побайтова копія `/opt/ugv-tilted-observe/20261117/package`: чинний serial-owner з інтегрованим штатним planar ExternalNav і виправленим збереженням ledger. Історична назва tilted не означає активний tilted-profile: `LEGACY_PLANAR_Z0_YAW_ONLY`, marker map v2, bench-profile не використовується.
- `config/rootfs/` — чинні base units, усі активні persistent/runtime drop-ins, allowlisted UID/GID/flag/socket env, nginx, tmpfiles, alignment grant, boot config та sole-owner helper. Це інертні копії; вони не встановлюються автоматично.
- `operations/` — фактично використані activation/channel helpers, current-boot HOME evidence та пасивний logger. Їхні початкові абсолютні шляхи/умови перевірок збережені. Це не переносний автоматичний installer.
- `evidence/` — прийняті звіти/JSON і bounded excerpts оригінальних рядків logger. `SOURCE.json` фіксує номери рядків та SHA256 джерела; великі повні mutable raw-журнали лишаються у `web_disarm_logs/` поза комітом.
- `provenance/` — побайтові хеші всіх скопійованих файлів, їхні джерела, права/UID/GID, effective services, installed dependency versions і boot ID.

Три tracked зміни UI у `hmi_disarm_prepared` збігаються з установленими assets. Супровідна локальна зміна `tests/browser_check.py` описує поточне компонування кнопок; фактично встановлений старий тест окремо збережений у snapshot. Ніякі тести при checkpoint не запускалися.

## Прийняті результати

Час — Київ, 17.11.2026. Відлік затримок починається з прийняття запиту owner, а не фізичного кліку.

| Випробування | ARMED → DISARMED у logger | До FULL_WRITE | До запису DISARMED owner | До DISARMED logger |
|---|---|---:|---:|---:|
| На місці | 19:21:13.961 → 19:21:17.253 | 39,819 мс | 177,768 мс | 519,370 мс |
| У русі | 19:28:06.992 → 19:28:19.121 | 48,085 мс | 870,857 мс | 1068,997 мс |

Оператор прийняв стаціонарний результат і підтвердив: «ровер зупинився після натискання» у русі; згодом підтвердив успішне завершення обох випробувань. Нові Web UUID: `b6853e32-6237-45d0-9551-084748b41687` і `a6cfc772-f126-4f05-bce3-305742198884`. В обох випадках SENT/FULL_WRITE, target1/1, далі свіжий post-send HEARTBEAT і причина `DISARMED_STATE_OBSERVED_NOT_COMMAND_PROOF`.

ACK цей logger не записує. Час фізичного кліку/відмальовування браузера, RPM, час і шлях фізичної зупинки не виміряно. FULL_WRITE не доводить приймання саме цього UUID Pixhawk. Попередній перехід о19:28:00.951 без нового Web UUID не зарахований як Web DISARM.

Наприкінці рухомого тесту зафіксовано VISION_POSITION health=false, коротке відновлення й подальшу втрату fresh_optical_pose. Ці факти наведені у звітах; milestone підтверджує Web DISARM, а не безперервну якість навігації на всьому маршруті. Додаткова діагностика не проводилася.

Результати обох тестів внесені та перевірені у [UGV — Daily Report](https://docs.google.com/document/d/1MjzgsjxEmxBSYiqDXTcW8tCqGjOSktMMK4gQaQ7z9Mk).

## Резервування та межі відновлення

Архів `backups/WEB_DISARM_VALIDATED_<UTC>.tar.gz` містить цей очищений snapshot та контрольні хеші; архів зберігається локально поза Git. Після коміту додається Git bundle з історією checkpoint і annotated tag `WEB_DISARM_VALIDATED`.

Паролі/хеші паролів, auth.key, TLS private keys, production auth/ledger DB, DB backups, приватні credentials, venv/cache та зайві тимчасові файли НЕ копіюються. Два env-файли містять лише перевірений allowlist числових identity/flags та socket path. Auth DB/key і TLS key мають лишатися у власному захищеному сховищі: ця копія конфігурації не замінює їх резервування та не відновить HTTPS/auth на порожньому хості самостійно.

На reboot `/run` overrides Web DISARM зникають і tmpfiles відновлює inhibit; Web DISARM знову OFF. Alignment grant та HOME evidence прив'язані до поточного boot/hash. Їх не можна копіювати як новий дозвіл після reboot. Відновлення не повинно переписувати production DB, повторювати історичні UUID, обходити preflight або автоматично вмикати TX/DISARM. Архів не містить команд автоматичного restore/deploy.

Перевірка snapshot: `sha256sum -c SHA256SUMS` у цьому каталозі. Список provenance дозволяє окремо звірити копії з установленими файлами без запуску служб чи випробувань.

## Наступне завдання: GPIO17 → STOP_LATCH → RESET у Web HMI

Див. `NEXT_GPIO17_INTEGRATION.md`. Робочу базу зафіксовано окремим milestone/tag; старий candidate не слід встановлювати поверх чинного owner цілком.
