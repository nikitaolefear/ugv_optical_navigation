"""Closed permission vocabulary and initial role policy, independent of hardware."""
PERMISSIONS = {
    'pixhawk.disarm': 'Штатний DISARM реального Pixhawk',
    'status.view': 'Перегляд стану DEMO',
    'telemetry.view': 'Перегляд телеметрії',
    'audit.view': 'Власний журнал подій',
    'audit.all': 'Журнал подій усіх користувачів',
    'statistics.view': 'Власна статистика',
    'statistics.all': 'Статистика всіх користувачів',
    'demo.control': 'Керування DEMO',
    'users.manage': 'Керування користувачами нижчого рангу',
    'roles.manage': 'Керування ролями нижчого рангу',
}
READ_PERMISSIONS = ('status.view', 'telemetry.view', 'audit.view', 'statistics.view')
DEFAULT_ROLES = {
    'superadmin': (1000, tuple(p for p in PERMISSIONS if p != 'pixhawk.disarm')),
    'admin': (300, tuple(p for p in PERMISSIONS if p != 'pixhawk.disarm')),
    'operator': (200, (*READ_PERMISSIONS, 'demo.control')),
    'observer': (100, READ_PERMISSIONS),
}


def initialize_roles(db):
    db.execute('CREATE TABLE roles(name TEXT PRIMARY KEY, rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 1000))')
    db.execute('CREATE TABLE role_permissions(role TEXT NOT NULL REFERENCES roles(name) ON DELETE CASCADE, '
               'permission TEXT NOT NULL, PRIMARY KEY(role,permission))')
    for name, (rank, permissions) in DEFAULT_ROLES.items():
        db.execute('INSERT INTO roles VALUES(?,?)', (name, rank))
        db.executemany('INSERT INTO role_permissions VALUES(?,?)', [(name, p) for p in permissions])
    db.execute('CREATE TABLE access_meta(version INTEGER NOT NULL)')
    db.execute('INSERT INTO access_meta VALUES(1)')


USERS_SQL = '''CREATE TABLE users(
    id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    role TEXT NOT NULL REFERENCES roles(name), active INTEGER NOT NULL CHECK(active IN (0,1)),
    created REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)))'''
