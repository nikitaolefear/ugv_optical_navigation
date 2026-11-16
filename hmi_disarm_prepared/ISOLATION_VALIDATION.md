# Перевірка доступного ізольованого середовища

Дата: 2026-11-16. Гілка `feature/ugv-hmi-disarm`, HEAD
`f4462926b40dc888a77b18e6fbce105dd0a776a5`.

**Повна cross-UID/GID та systemd перевірка заблокована середовищем.**
Виконані функціональні регресії в апаратно ізольованому single-UID
bubblewrap sandbox. Це не заміна потрібного cross-UID/systemd acceptance test.

## Доступність середовища

Перевірки не змінювали users/groups, /etc/subuid, unit-файли чи cgroup хоста.

| Проба | Фактичний результат |
| --- | --- |
| Rootless user namespace | Доступний, але тільки один mapped UID/GID |
| Спроба namespace-local setuid(1) | EINVAL: UID 1 не mapped; хостовий UID не змінювався |
| Multi-ID `unshare --map-auto` | `failed to execute newuidmap: No such file or directory` |
| newuidmap/newgidmap | Не знайдені, хоча subordinate ranges прописані |
| setgroups усередині доступного sandbox | `deny` |
| cgroup2 хоста | Немає writable delegation поточному користувачу |
| docker/podman/systemd-nspawn | Не знайдені |
| qemu-system-aarch64/x86_64/riscv64 | Не знайдені; VM не встановлювалася й не завантажувалася |
| Sandbox PID1 | `bwrap`, не systemd |

Значення map у фактичному sandbox:

```text
uid_map: 1000 0 1
gid_map: 1000 0 1
setgroups: deny
```

Overflow group 65534 у відображенні невідомих namespace-груп не є
окремою сконфігурованою RPC-групою і не доводить ізоляції між користувачами.
Нові OS users/groups, helpers, container engine чи transient host units не
створювалися. Не виконувались sudo-install, useradd, chmod/chown робочих
каталогів, налаштування writable host cgroup або host systemd-run.

## Фактично виконаний sandbox-прогін

Використано тимчасові user/PID/mount/network/IPC/UTS/cgroup namespaces.
Root filesystem і workspace read-only. /dev синтетичний; /run, /sys, /tmp
приватні. /home прихований, крім read-only package workspace та test venv.
Єдиний додатковий writable bind — disposable /tmp directory для звіту.
Жодного physical serial FD або host bus socket не передано.

Шість окремих boundary assertions пройшли:

1. Немає /dev/serial та ttyACM-пристроїв.
2. Немає host /run/systemd/private.
3. Немає host system D-Bus socket.
4. Немає host cgroup tree в /sys/fs/cgroup.
5. Workspace має read-only filesystem flag.
6. Production auth state /home/ugvs2/.local/state/ugv-hmi прихований.

Перша спроба розмістити harness mount point у read-only / завершилась
безпечною відмовою. Harness та artifacts розміщено в приватному /tmp;
root filesystem не робився writable для обходу цього обмеження.

Після перевірки boundary assertions виконано незмінений run_checks.py.
**253/253 наявні Python-регресії пройшли всередині sandbox.**

| Набір | Пройшло |
| --- | ---: |
| Engine/MAVLink delivery і observations | 43 |
| Unix IPC | 14 |
| Gateway relay/policy | 14 |
| Inhibit logical behavior | 4 |
| Статичний deployment profile | 3 |
| Owner/HoldBench integration | 6 |
| Owner CLI validation | 3 |
| Legacy read-only routes | 5 |
| HMI auth/CSRF/audit/API | 25 |
| Flask → gateway → owner → fake writer | 1 |
| Auth/access/DEMO регресії | 34 + 22 + 18 |
| Незмінене production STOP | 61 |

Не додавати 6 boundary assertions до Python-count і не називати суму
«259 архітектурними тестами». **Повноцінних cross-UID/systemd тестів: 0.**
34 браузерні перевірки залишаються результатом попереднього етапу;
у цьому етапі браузер не запускався.

Артефакти сесії:

- `/tmp/ugv-disarm-namespace-results/namespace.json` — фактичні maps і boundary results;
- `/tmp/ugv-disarm-namespace-results/python-tests.json` — результати Python-груп;
- `/tmp/ugv-disarm-namespace-check.py` — тимчасовий harness, не production component.

## Покриття запитаних сценаріїв

| Сценарій | Що підтверджено | Що не підтверджено |
| --- | --- | --- |
| Fake serial-owner / gateway | Наявні library/IPC/e2e регресії в sandbox, без обладнання | Окремі service UID і реально застосований systemd profile |
| UID/GID, socket/dir/journal DAC | Schema/mode/peer policy та same-UID fixtures | Справжній cross-UID доступ/відмова, supplementary groups, journald rights |
| Неавторизований клієнт | RBAC/CSRF та програмні negative peer checks | Kernel DAC/SO_PEERCRED між різними mapped користувачами |
| Nonce/replay/repeated POST/errors | Функціональні регресії, durable UUID та підміни відмов | Поведінка separate-UID service topology під supervisor |
| Restart/crash | Library owner reconstruction/DB no-replay cases | SIGKILL/restart реальних owner/gateway systemd units, stale-socket lifecycle |
| DISARMED_OBSERVED/freshness | Fake trusted heartbeat; stale/re-ARM/link loss → UNKNOWN | Справжнє обладнання, канальна достовірність і hard-real-time latency |
| Inhibit/rollback state | Логічний inhibit та збереження temporary ledger/identities | Root-owned policy DAC і реальний cross-UID rollback/ownership migration |
| Legacy 8084 | Prepared Flask GET працює, mutation methods відхилені | Live 8084 не тестувався й не змінювався |

## Дефекти й залишкові ризики

Нових функціональних дефектів **у доступному обсязі перевірки** не виявлено.
Загальне твердження «архітектура пройшла cross-UID/systemd аудит» неприпустиме.

Не підтверджені: перехід між реальними UID/GID, доступ до журналів і state,
заборона gateway на AF_INET/devices/sudo саме застосованим systemd sandbox,
поведінка сервісів при kill/restart та безпечний cleanup stale Unix socket.
Поточний код навмисно не видаляє наявний socket при start; це fail-closed
policy, але потрібна перевірена supervisor/operations процедура для crash recovery.

Гарантії не можна отримати через fakeroot/proot, кілька single-ID namespaces,
патч перевірок UID, systemd --user або статичне читання unit-файлу.
Ці варіанти не використовувалися як заміна повноцінного тесту.

## Готовність і наступний етап

Пакет готовий лише до **наступного ізольованого VM/container тестового етапу**.
Production/hardware інтеграція залишається заблокованою.

Потрібне окреме середовище з private /dev/network/host-bus boundary,
щонайменше чотирма mapped UID (frontend/gateway/owner/attacker), двома RPC
GID, справжніми setgroups/setresuid/setresgid/chown і nested systemd PID1
із writable delegated cgroup subtree. Або disposable VM із власними UID/cgroups.
Це має бути надано без зміни робочої UGV-системи в цьому завданні.

Там виконати: attacker без GID та з GID але неправильним UID; frontend
не має backend/state доступу; клієнт не може замінити socket; gateway не
відкриває devices/TCP/sudo і не отримує FD; journal DAC; actual nonce replay,
socket replacement/loss; owner/gateway kill/restart; inhibit до write;
збереження ledger/audit і UNKNOWN після restart/telemetry loss; legacy POST denial.

Робочі користувачі, групи, служби, unit-файли, identity DB, права,
HMI, GPIO, Pixhawk, VESC та фізичний E-STOP не змінені. Deployment,
staging, commit і push не виконувались. Жодних MAVLink-команд чи serial open.
