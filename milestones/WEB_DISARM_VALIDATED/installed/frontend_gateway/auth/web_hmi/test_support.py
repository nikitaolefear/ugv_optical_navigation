"""Real login/CSRF test fixture; no production authentication bypass."""
import re
import tempfile
from .auth import AuthStore
from .app import create_app

PASSWORD = 'fixture-only-0000000005'
CONFIG = dict(TESTING=True, PUBLIC_ORIGIN='http://localhost', COOKIE_SECURE=False)


class TestEnvironment:
    def __init__(self, clock=None):
        self.tmp = tempfile.TemporaryDirectory(prefix='ugv-auth-test-')
        self.store = AuthStore(self.tmp.name, **({'clock':clock} if clock else {}))
        self.store.add_user('testadmin', PASSWORD, 'admin', first=True)

    def close(self):
        self.tmp.cleanup()

    def app(self, sim=None, telemetry=None):
        return create_app(sim, telemetry, config=CONFIG, auth_store=self.store)

    def client(self, sim=None, telemetry=None):
        client = self.app(sim, telemetry).test_client()
        page = client.get('/login')
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        result = client.post('/login', data=dict(username='testadmin', password=PASSWORD, csrf_token=token))
        assert result.status_code == 303
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', client.get('/').text).group(1)
        return CSRFClient(client, token)


class CSRFClient:
    def __init__(self, client, csrf):
        self.client, self.csrf = client, csrf

    def get(self, *args, **kwargs):
        return self.client.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers.setdefault('X-CSRF-Token', self.csrf)
        return self.client.post(*args, headers=headers, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
