"""Synthetic values only; never print passwords, tokens or password hashes."""
from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import os
import pty
import re
import secrets
import select
import signal
import string
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash
from web_hmi import admin_cli
from web_hmi.app import create_app
from web_hmi.auth import AccountError, AuthStore, PASSWORD_METHOD, MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH, password_hash
from web_hmi.test_support import CONFIG


def synthetic(length, alphabet=string.ascii_letters):
    return ''.join(secrets.choice(alphabet) for _ in range(length))


class PasswordPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ugv-password-policy-test-')
        self.addCleanup(self.tmp.cleanup)
        self.store=AuthStore(self.tmp.name)

    def test_policy_constants_and_scrypt_unchanged(self):
        self.assertEqual((MIN_PASSWORD_LENGTH,MAX_PASSWORD_LENGTH),(8,128))
        self.assertEqual(PASSWORD_METHOD,'scrypt:32768:8:1')

    def test_hash_boundaries_and_no_truncation(self):
        for length in (7,8,128,129):
            with self.subTest(length=length):
                value=synthetic(length)
                if length in (7,129):
                    with self.assertRaisesRegex(AccountError,'8.*128'):
                        password_hash(value)
                else:
                    encoded=password_hash(value)
                    self.assertTrue(encoded.startswith(PASSWORD_METHOD+'$'))
                    self.assertTrue(check_password_hash(encoded,value))
        value=synthetic(128)
        encoded=password_hash(value)
        self.assertFalse(check_password_hash(encoded,value+synthetic(1)))
        for invalid in (None,8,[],b''):
            with self.subTest(kind=type(invalid).__name__),self.assertRaises(AccountError):
                password_hash(invalid)

    def test_create_user_enforces_both_boundaries(self):
        for length in (7,8,128,129):
            with self.subTest(length=length):
                value=synthetic(length)
                name='fixture'+str(length)
                if length in (7,129):
                    with self.assertRaises(AccountError): self.store.add_user(name,value,'operator')
                else:
                    self.store.add_user(name,value,'operator')
                    with self.store.transaction() as db:
                        encoded=db.execute('SELECT password_hash FROM users WHERE username=?',(name,)).fetchone()[0]
                    self.assertTrue(check_password_hash(encoded,value))
        self.assertEqual(len(self.store.users()),2)

    def test_change_password_enforces_boundaries_and_reject_does_not_mutate(self):
        self.store.add_user('fixtureadmin',synthetic(24),'admin',first=True)
        for length in (7,8,128,129):
            with self.subTest(length=length):
                with self.store.transaction() as db:
                    before=db.execute('SELECT password_hash FROM users').fetchone()[0]
                value=synthetic(length)
                if length in (7,129):
                    with self.assertRaises(AccountError): self.store.change_user('fixtureadmin',password=value)
                    with self.store.transaction() as db:
                        after=db.execute('SELECT password_hash FROM users').fetchone()[0]
                    self.assertTrue(before==after)
                else:
                    self.store.change_user('fixtureadmin',password=value)
                    with self.store.transaction() as db:
                        encoded=db.execute('SELECT password_hash FROM users').fetchone()[0]
                    self.assertTrue(check_password_hash(encoded,value))

    def test_character_types_have_no_composition_requirements(self):
        cases=[('letters',synthetic(8)),('digits',synthetic(8,string.digits)),
               ('punctuation',synthetic(8,string.punctuation)),
               ('cyrillic',synthetic(8,'абвгдеєжзиіїйклмнопрстуфхцчшщьюя')),
               ('mixed',synthetic(16,string.ascii_letters+string.digits+' '+string.punctuation)),
               ('all_spaces',' '*8),('edge_spaces','  '+synthetic(4)+'  '),
               ('non_bmp',synthetic(8,'🛰🚙🔐🛞'))]
        for kind,value in cases:
            with self.subTest(kind=kind):
                encoded=password_hash(value)
                self.assertTrue(check_password_hash(encoded,value))
                if value != value.strip():
                    self.assertFalse(check_password_hash(encoded,value.strip()))
                if kind=='non_bmp':
                    self.assertEqual(len(value),8)
        value=synthetic(128,'🛰🚙🔐🛞')
        self.assertTrue(check_password_hash(password_hash(value),value))

    def test_cli_returns_exact_hidden_value_and_uses_new_prompt(self):
        value='  '+synthetic(4)+'  '
        output=io.StringIO()
        with patch.object(admin_cli.sys.stdin,'isatty',return_value=True), \
             patch.object(admin_cli.getpass,'getpass',side_effect=[value,value]) as prompt, \
             redirect_stdout(output),redirect_stderr(output):
            received=admin_cli.read_password()
        self.assertTrue(received==value)
        self.assertEqual(prompt.call_count,2)
        self.assertIn('8–128',prompt.call_args_list[0].args[0])
        self.assertFalse(value in output.getvalue())

    def test_cli_mismatch_and_non_tty_do_not_echo(self):
        value=synthetic(8)
        output=io.StringIO()
        with patch.object(admin_cli.sys.stdin,'isatty',return_value=True), \
             patch.object(admin_cli.getpass,'getpass',side_effect=[value,synthetic(8)]), \
             redirect_stdout(output),redirect_stderr(output):
            with self.assertRaises(AccountError): admin_cli.read_password()
        self.assertFalse(value in output.getvalue())
        with patch.object(admin_cli.sys.stdin,'isatty',return_value=False), \
             patch.object(admin_cli.getpass,'getpass') as prompt:
            with self.assertRaises(AccountError): admin_cli.read_password()
            prompt.assert_not_called()

    def test_login_hint_does_not_clip_or_impose_new_minimum_on_legacy_login(self):
        app=create_app(config=CONFIG,auth_store=self.store)
        page=app.test_client().get('/login')
        self.assertEqual(page.status_code,200)
        self.assertIn('8–128',page.text)
        tag=re.search(r'<input[^>]*name="password"[^>]*>',page.text).group(0)
        self.assertIn('type="password"',tag)
        self.assertIn('autocomplete="current-password"',tag)
        self.assertNotIn('maxlength',tag)
        self.assertNotIn('minlength',tag)
        self.assertNotIn('pattern=',tag)

    def test_new_and_previously_stored_hashes_login_without_rehash(self):
        previous=synthetic(32)
        legacy_hash=generate_password_hash(previous,method='scrypt:32768:8:1')
        # A pre-existing account fixture only, never a deployment/private database.
        self.store.add_user('fixtureadmin',synthetic(20),'admin',first=True)
        with self.store.transaction() as db:
            db.execute('UPDATE users SET password_hash=? WHERE username=?',(legacy_hash,'fixtureadmin'))
        token,status=self.store.login('fixtureadmin',previous,'127.0.0.1',self.store.anonymous_session())
        self.assertEqual(status,200)
        self.assertIsNotNone(self.store.session(token))
        with self.store.transaction() as db:
            persisted=db.execute('SELECT password_hash FROM users').fetchone()[0]
        self.assertTrue(persisted==legacy_hash)
        for length in (8,128):
            with self.subTest(length=length):
                value=' '+synthetic(length-2)+' '
                self.store.change_user('fixtureadmin',password=value)
                token,status=self.store.login('fixtureadmin',value,'127.0.0.1',self.store.anonymous_session())
                self.assertEqual(status,200)
                self.assertIsNotNone(self.store.session(token))
                _,status=self.store.login('fixtureadmin',value.strip(),'127.0.0.1',self.store.anonymous_session())
                self.assertEqual(status,401)

    def test_http_login_accepts_exact_spaces_special_and_unicode_boundaries(self):
        self.store.add_user('fixtureadmin',synthetic(24),'admin',first=True)
        app=create_app(config=CONFIG,auth_store=self.store)
        cases=[('digits',synthetic(8,string.digits)),('spaces',' '*8),
               ('edge_spaces',' '+synthetic(6)+' '),
               ('special_128',synthetic(128,string.punctuation)),
               ('unicode_128',synthetic(128,'абвієї🛰🚙'))]
        for kind,value in cases:
            with self.subTest(kind=kind):
                self.store.change_user('fixtureadmin',password=value)
                client=app.test_client()
                page=client.get('/login')
                csrf=re.search(r'name="csrf_token" value="([^\"]+)"',page.text).group(1)
                result=client.post('/login',data=dict(username='fixtureadmin',password=value,csrf_token=csrf))
                self.assertEqual(result.status_code,303)
                self.assertEqual(client.get('/api/status').status_code,200)

    def test_no_secret_values_or_hashes_in_audit(self):
        original=synthetic(8)
        replacement=' '+synthetic(6)+' '
        self.store.add_user('fixtureadmin',original,'admin',first=True)
        self.store.change_user('fixtureadmin',password=replacement)
        with self.store.transaction() as db:
            rows=[tuple(row) for row in db.execute('SELECT * FROM audit')]
            encoded=db.execute('SELECT password_hash FROM users').fetchone()[0]
        text=repr(rows)
        self.assertFalse(original in text)
        self.assertFalse(replacement in text)
        self.assertFalse(encoded in text)


class HiddenTerminalTest(unittest.TestCase):
    def test_real_pseudoterminal_getpass_disables_echo_and_preserves_spaces(self):
        # Only read_password(), no CLI main/DB/account/hardware entry point.
        value=' '+synthetic(6)+' '
        script=("from web_hmi.admin_cli import read_password; "
                "value=read_password(); "
                "ok=(len(value)==8 and value.startswith(' ') and value.endswith(' ')); "
                "print('HIDDEN_INPUT_OK' if ok else 'HIDDEN_INPUT_BAD'); "
                "raise SystemExit(0 if ok else 2)")
        pid,master=pty.fork()
        if pid==0:
            os.execl(sys.executable,sys.executable,'-B','-c',script)
        transcript=bytearray()
        deadline=time.monotonic()+10
        reaped=False
        status=None
        try:
            for marker in ('Новий пароль','Повторіть пароль'):
                needle=marker.encode()
                while needle not in transcript:
                    if time.monotonic()>deadline: self.fail('Synthetic getpass prompt timeout')
                    if select.select([master],[],[],.1)[0]:
                        try: data=os.read(master,4096)
                        except OSError: self.fail('Synthetic getpass terminal closed early')
                        if not data: self.fail('Synthetic getpass terminal ended early')
                        transcript.extend(data)
                os.write(master,value.encode()+b'\n')
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    try: data=os.read(master,4096)
                    except OSError as exc:
                        if exc.errno==errno.EIO: break
                        raise
                    if not data: break
                    transcript.extend(data)
            child,status=os.waitpid(pid,os.WNOHANG)
            while child==0 and time.monotonic()<deadline:
                time.sleep(.01)
                child,status=os.waitpid(pid,os.WNOHANG)
            if child==0:
                self.fail('Synthetic getpass did not finish')
            reaped=True
            self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status)==0)
            self.assertTrue(b'HIDDEN_INPUT_OK' in transcript)
            self.assertFalse(value.encode() in transcript,'Synthetic input must never be echoed')
        finally:
            if not reaped:
                try: os.kill(pid,signal.SIGKILL)
                except ProcessLookupError: pass
                os.waitpid(pid,0)
            os.close(master)
