# Проєкт вузького перевіряльника власників Pixhawk

СТАН: підготовлено для перегляду, НЕ встановлено. Інтеграційний патч НЕ
застосовано до робочих app.py та pixhawk_telemetry.py. Нові модулі не
викликаються чинною HMI. Sudoers, служби, /proc та сервери не змінювалися.

## Контракт і довіра

Root виконує лише `/usr/local/libexec/ugv-pixhawk-owners`, без аргументів.
Скрипт автономний: `#!/usr/bin/python3 -I`, лише стандартна бібліотека;
немає імпорту коду HMI, читання stdin, конфігурації з environment,
subprocess/shell, відкриття tty, ioctl, MAVLink, керування процесами або
запису файлів. Є тільки stat/lstat/readlink/listdir/scandir та JSON у stdout.
Ізольований Python і NOSETENV виключають підміну імпортів через PYTHONPATH
або робочий каталог. Змінні середовища не визначають шляхи чи поведінку.

Пристрій зашито в коді:
`/dev/serial/by-id/usb-Holybro_Pixhawk6C_260025000C51333336373039-if02`.
Від HMI не передаються навіть PID HMI чи ім'я порту. Аргументи заборонені
і правилом sudoers, і самим скриптом. Правило надає ugvs2 лише цю додаткову
можливість; воно не скасовує інших уже наявних адміністративних прав.

Перевіряються root-owned каталоги /dev, /dev/serial, /dev/serial/by-id без
запису для group/other, root-owned by-id symlink і root-owned character
node /dev/ttyACM<number>. Це покладається на коректну host udev-конфігурацію;
серійний номер USB не є криптографічною автентифікацією обладнання.

Власники визначаються за character-device st_rdev через stat /proc/PID/fd/N,
тому альтернативне ім'я пристрою, symlink або успадкований FD не приховують
власника. Інші пристрої не стають запитуваними цілями. Відсутні /proc PID/FD
пропускаються як нормальне завершення процесу/закриття дескриптора.
Інші помилки дають complete=false, без часткового списку власників.
Час сканування обмежений SIGALRM на 2 с; зовнішній клієнт має timeout 3 с.
Невизначеність або тайм-аут ніколи не перетворюються на «власників немає».

Призначено лише для запуску на самому хості ровера зі звичайним повним
host /proc та достатніми root capabilities. Не запускати у контейнері,
окремому PID namespace або з обмеженим /proc: неможливо виявити процеси,
які взагалі не видимі в namespace. Виявлені відмови доступу блокують перевірку.
Глобальний атомарний знімок /proc Linux не гарантує.

## Структурований результат

Exit 0 — перевірку завершено, навіть якщо owners непорожній:

```json
{"schema":1,"port":"/dev/serial/by-id/usb-Holybro_Pixhawk6C_260025000C51333336373039-if02","complete":true,"identity":{"target":"/dev/ttyACM1","dev":123,"ino":456,"rdev":789},"owners":[6405],"error":null}
```

Числа identity у прикладі умовні. Вони отримуються до і після обходу /proc;
зміна пристрою дає DEVICE_CHANGED, а не успішний результат.

Exit 1 — помилка, complete=false, identity=null, owners=[]:

```json
{"schema":1,"port":"/dev/serial/by-id/usb-Holybro_Pixhawk6C_260025000C51333336373039-if02","complete":false,"identity":null,"owners":[],"error":{"code":"PermissionError","path":"/proc/1/fd","errno":13,"message":"[Errno 13] Permission denied: '/proc/1/fd'"}}
```

Інші коди: ROOT_REQUIRED, ARGUMENTS_FORBIDDEN, UNTRUSTED_DEVICE_PATH,
UNEXPECTED_DEVICE, DEVICE_CHANGED, SCAN_TIMEOUT або назва OSError.
Клієнт строго перевіряє схему, fixed port, complete, exit code, stderr,
типи PID/identity, дублікати та обмеження JSON 64 KiB. Відмова sudo,
відсутній executable, тайм-аут чи пошкоджений JSON → PORT_CHECK_FAILED.

## Інтеграція з HMI

Патч додає опційний owner_check до open_passive та CLI-прапорець
--telemetry-owner-helper. Без прапорця поточний backend /proc+fuser
залишається без змін. З прапорцем перевірки власників виконує новий helper,
який сам сканує /proc і не залежить від fuser. Відмови не спричиняють fallback.

Виклик клієнта фіксований:

```python
subprocess.run(
    ('/usr/bin/sudo', '-n', '--', '/usr/local/libexec/ugv-pixhawk-owners'),
    stdin=subprocess.DEVNULL, capture_output=True, timeout=3,
    shell=False, close_fds=True, cwd='/',
    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
)
```

Послідовність: helper до відкриття → лише порожній owners → O_RDONLY
open → flock(LOCK_EX|LOCK_NB) → TIOCEXCL → helper після відкриття →
лише PID HMI → перевірка fstat відкритого FD та identity обох знімків →
налаштування tty → пасивне читання. Дескриптор не успадковується helper-ом.
Сторонній PID/конфлікт блокування → PORT_BUSY; помилка перевірки →
PORT_CHECK_FAILED; відмова доступу до tty → PORT_ACCESS_DENIED.
Будь-яка відмова після відкриття закриває FD до читання байтів.
Flask увесь час залишається ugvs2; root тільки короткочасний перевіряльник.

JSON helper журналюється як owner_helper на INFO. При відмові чинний
ERROR-журнал HMI містить traceback і первинний result; поля fuser_rc/
fuser_stdout/fuser_stderr збережені для сумісності та в цьому backend
містять exit code/JSON/stderr helper, а не реальний виклик fuser.

## Залишкове вікно конкуренції

Між завершенням першого сканування та open, а також між open і TIOCEXCL
інший процес може відкрити tty. flock координує тільки тих, хто також
його використовує. TIOCEXCL забороняє наступні звичайні відкриття, але
не анулює вже відкриті FD. Друге сканування виявляє таких власників і
HMI відмовляється читати. Ідентичність через fstat також обмежує ризик
перепідключення/перепризначення пристрою між перевірками.

Процес може відкрити, прочитати і закрити tty повністю між знімками:
це неможливо надійно виявити цим перевіряльником. Процеси з CAP_SYS_ADMIN
можуть обходити TIOCEXCL, а успадкований відкритий FD не потребує нового
open. Helper не гарантує безперервної ексклюзивності та не відновлює
повідомлення, втрачені через іншого читача. Потрібна організаційна умова:
не запускати інших serial-клієнтів паралельно. Для спільного використання
потрібен один постійний власник serial (MAVLink router) та окремий канал HMI.

Джерела семантики: sudoers(5), правило без аргументів:
https://www.sudo.ws/docs/man/1.9.14/sudoers.man.pdf ;
TIOCEXCL/CAP_SYS_ADMIN:
https://man7.org/linux/man-pages/man2/tiocgexcl.2const.html .

## Тести без обладнання

Усі syscall, сканування /proc, root identity і запуск sudo у тестах підмінені.
Системний helper жодного разу не запускається. У робочому дереві:

```bash
cd /home/ugvs2/ugv_optical_navigation
PYTHONDONTWRITEBYTECODE=1 /home/ugvs2/.venv/bin/python -m unittest \
  web_hmi.test_hmi web_hmi.test_pixhawk_telemetry web_hmi.test_pixhawk_owner_helper -v
```

Очікувано 74 успішні, 3 інтеграційні skip до застосування патча.
Застосований тільки в /tmp/ugv-owner-helper-review патч пройшов 77/77,
без skip. Перевірено no-args/non-root, fixed device, довіру до шляхів,
PID/rdev, помилки /proc, зникнення процесу, заміну пристрою, timeout,
некоректний JSON/sudo failure, власний PID, O_RDONLY/обидва блокування,
перевірки до/після і відмову від open/read після невизначеності.
Dry-run патча успішний. Локальний sudoers-шаблон: visudo parsed OK;
середовище інструментів окремо видало попередження про uid /etc/sudo.conf.
Повну перевірку конфігурації sudo на ровері ще треба виконати.

## Встановлення — тільки після окремого погодження, НЕ виконано

Передумови: host Linux ровера, системні /usr/bin/python3, /usr/bin/sudo,
/usr/sbin/visudo та patch; ugvs2 має dialout. Код і ці інструкції переглянуті.
/usr, /usr/local і /usr/local/libexec повинні бути root-owned та недоступні
для запису ugvs2/group/other. Якщо це не так — зупинитися; не встановлювати
привілейований скрипт у каталог користувача чи venv. Нові системні імена
нижче мають бути відсутні; існуючі компоненти не перезаписувати.

Команди для адміністратора (не запускають helper, HMI чи Pixhawk):

```bash
set -eu
cd /home/ugvs2/ugv_optical_navigation

# Перевірка патча і резервна копія лише двох змінюваних файлів HMI.
patch --dry-run --fuzz=0 -p1 < web_hmi/privileged/hmi_integration.patch
mkdir web_hmi/backups/before-owner-helper
cp -p web_hmi/app.py web_hmi/pixhawk_telemetry.py web_hmi/backups/before-owner-helper/

# Перевірити власника/права кожного компонента шляху; якщо каталог
# libexec відсутній, створити його. Не продовжувати при writable parent.
if [ ! -d /usr/local/libexec ]; then
    sudo install -d -o root -g root -m 0755 /usr/local/libexec
fi
namei -l /usr/local/libexec
sudo test ! -e /usr/local/libexec/ugv-pixhawk-owners
sudo test ! -L /usr/local/libexec/ugv-pixhawk-owners
sudo test ! -e /etc/sudoers.d/ugv-pixhawk-owners
sudo test ! -L /etc/sudoers.d/ugv-pixhawk-owners
sudo test ! -e /etc/sudoers.d/.ugv-pixhawk-owners.pending
sudo test ! -L /etc/sudoers.d/.ugv-pixhawk-owners.pending

# root-owned автономна копія, а не symlink у робоче дерево.
sudo install -o root -g root -m 0755 web_hmi/privileged/pixhawk_owners.py /usr/local/libexec/ugv-pixhawk-owners
sudo install -o root -g root -m 0440 web_hmi/privileged/ugv-pixhawk-owners.sudoers /etc/sudoers.d/.ugv-pixhawk-owners.pending
sudo /usr/sbin/visudo -cf /etc/sudoers.d/.ugv-pixhawk-owners.pending
sudo mv -- /etc/sudoers.d/.ugv-pixhawk-owners.pending /etc/sudoers.d/ugv-pixhawk-owners
sudo /usr/sbin/visudo -c

# Інтеграція на диску; debug/reloader вимкнено, процеси не перезапускаються.
patch --fuzz=0 -p1 < web_hmi/privileged/hmi_integration.patch
PYTHONDONTWRITEBYTECODE=1 /home/ugvs2/.venv/bin/python -m unittest \
  web_hmi.test_hmi web_hmi.test_pixhawk_telemetry web_hmi.test_pixhawk_owner_helper -v
```

Якщо будь-який крок неуспішний — не запускати HMI; виконати відповідні
кроки відкату нижче. У встановленні немає змін служб чи запусків серверів.
Sudoers не містить дозволів на python, sh, fuser або довільні аргументи:

```sudoers
ugvs2 ALL=(root) NOPASSWD: NOSETENV: /usr/local/libexec/ugv-pixhawk-owners ""
```

Окрема пасивна перевірка helper ПІСЛЯ погодження встановлення, від ugvs2:

```bash
sudo -n -- /usr/local/libexec/ugv-pixhawk-owners
```

Вона лише читає метадані /proc і пристрою, не відкриває tty.
Запуск HMI нижче вже відкриває Pixhawk і потребує окремого погодження
та вільного HTTP-порту. Показано як приклад, зараз НЕ виконувати:

```bash
/home/ugvs2/.venv/bin/python web_hmi/app.py --host 127.0.0.1 --port 8082 --telemetry --telemetry-owner-helper
```

Не додавати sudo перед Flask. Чинний сервер не перемикається автоматично.

## Відкат — команди, НЕ виконано

Спершу прибрати тільки доданий дозвіл і його executable. Каталог libexec
і будь-які інші sudoers-файли залишити без змін:

```bash
set -eu
cd /home/ugvs2/ugv_optical_navigation
sudo rm -f -- /etc/sudoers.d/ugv-pixhawk-owners /etc/sudoers.d/.ugv-pixhawk-owners.pending
sudo /usr/sbin/visudo -c
sudo rm -f -- /usr/local/libexec/ugv-pixhawk-owners

# Виконати, якщо створено резервну копію при встановленні:
cp -p web_hmi/backups/before-owner-helper/app.py web_hmi/app.py
cp -p web_hmi/backups/before-owner-helper/pixhawk_telemetry.py web_hmi/pixhawk_telemetry.py
```

Нові файли-проєкти client/helper/tests можна залишити: без патча вони
не підключені. Резервну копію не видаляти до перевірки відкату.
Відкат файлів та дозволу не змінює вже завантажений код процесів і не
закриває serial-дескриптор активного HMI. Якщо новий backend уже був
запущений, його контрольована зупинка/повернення попереднього HMI
потребують окремого погодження. Ці команди самі сервери не перезапускають.
