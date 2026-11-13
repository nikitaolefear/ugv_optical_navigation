"""Local trusted-OS-user CLI. No passwords in command arguments/environment."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import warnings
import sqlite3
from .auth import AccountError, AuthStore, MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH


def read_password():
    # getpass otherwise falls back to echoed stdin when there is no controlling tty.
    if not sys.stdin.isatty():
        raise AccountError('Для прихованого введення пароля потрібен локальний термінал.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            first = getpass.getpass(f'Новий пароль ({MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} символів): ')
            second = getpass.getpass('Повторіть пароль: ')
    except getpass.GetPassWarning:
        raise AccountError('Неможливо вимкнути відображення пароля в цьому терміналі.') from None
    if first != second:
        raise AccountError('Паролі не збігаються.')
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=os.environ.get('UGV_HMI_STATE_DIR', str(Path.home()/'.local/state/ugv-hmi')))
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('bootstrap-admin', 'create', 'password', 'enable', 'disable', 'role', 'revoke'):
        command = sub.add_parser(action)
        command.add_argument('username')
        if action in ('role', 'create'):
            command.add_argument('role')
    sub.add_parser('migrate-access')
    grant = sub.add_parser('grant-superadmin')
    grant.add_argument('username')
    sub.add_parser('list')
    sub.add_parser('prune')
    sub.add_parser('csrf-diagnostics')
    args = parser.parse_args(argv)
    try:
        if args.action == 'migrate-access':
            from .access_migration import migrate_access
            changed = migrate_access(args.state_dir)
            print('Міграцію доступу виконано.' if changed else 'Схема доступу вже актуальна.')
            return 0
        store = AuthStore(args.state_dir, read_only=args.action == 'csrf-diagnostics')
        if args.action == 'csrf-diagnostics':
            diagnostics = store.csrf_diagnostics()
            if not diagnostics['records']:
                diagnostics['message'] = 'Валідних записів csrf.denied серед останніх 100 подій немає.'
            print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
            return 0
        if args.action == 'grant-superadmin':
            if not any(u['username'] == args.username.lower() and u['active'] for u in store.users()):
                raise AccountError('Потрібен наявний активний обліковий запис; пароль і активність команда не змінює.')
            store.change_user(args.username, role='superadmin')
        elif args.action == 'bootstrap-admin':
            store.add_user(args.username, read_password(), 'admin', first=True)
        elif args.action == 'create':
            store.add_user(args.username, read_password(), args.role)
        elif args.action == 'list':
            print(json.dumps(store.users(), ensure_ascii=False, indent=2))
        elif args.action == 'prune':
            store.audit(None, 'audit.prune', 'OK')
            with store.transaction() as db:
                db.execute('DELETE FROM sessions WHERE created<=? OR seen<=?',
                           (store.clock()-store.lifetime, store.clock()-store.idle))
            # Incremental bounded retention runs on every audit; offline VACUUM reclaims pages.
        else:
            options = {'password': read_password()} if args.action == 'password' else (
                {'role': args.role} if args.action == 'role' else
                {'active': args.action == 'enable'} if args.action in ('enable', 'disable') else {'revoke': True})
            store.change_user(args.username, **options)
        return 0
    except AccountError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, RuntimeError, sqlite3.Error):
        print('Не вдалося відкрити приватне сховище HMI.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
