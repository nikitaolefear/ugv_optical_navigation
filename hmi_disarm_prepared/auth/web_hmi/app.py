"""UGV HMI: authenticated DEMO and independent passive telemetry."""
import argparse
import atexit
from collections import OrderedDict
from threading import RLock
import hmac
import json
import secrets
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, redirect, render_template, request
from werkzeug.exceptions import SecurityError
try:
    from .auth import AuthStore, diagnostic_origin, AccountError, AccessDenied
    from .access_control import PERMISSIONS
    from .mission_statistics import MissionStatistics, PERIODS
    from .simulator import Simulator, TransitionError
    from .pixhawk_telemetry import PixhawkTelemetry
    from .telemetry_bridge import TelemetryBridge
except ImportError:
    from auth import AuthStore, diagnostic_origin, AccountError, AccessDenied
    from access_control import PERMISSIONS
    from mission_statistics import MissionStatistics, PERIODS
    from simulator import Simulator, TransitionError
    from pixhawk_telemetry import PixhawkTelemetry
    from telemetry_bridge import TelemetryBridge

COOKIE = 'ugv_session'
DEMO_ACTIONS = {'select', 'start', 'pause', 'resume', 'stop', 'hold', 'reset', 'fault', 'estop'}
LOGIN_ERROR = 'Не вдалося увійти. Перевірте дані або спробуйте пізніше.'


def create_app(simulator=None, telemetry=None, *, config=None, auth_store=None, disarm_client=None):
    if os.geteuid() == 0:
        raise RuntimeError('Flask HMI must run as an unprivileged OS user')
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=4096,
        AUTH_STATE_DIR=os.environ.get('UGV_HMI_STATE_DIR', str(Path.home()/'.local/state/ugv-hmi')),
        PUBLIC_ORIGIN=os.environ.get('UGV_HMI_PUBLIC_ORIGIN', 'https://192.168.110.59'),
        COOKIE_SECURE=True, IDLE_SECONDS=900, MAX_SESSION_SECONDS=28800,
        DISARM_ENABLED=os.environ.get('UGV_HMI_ENABLE_DISARM') == '1',
        DISARM_GATEWAY_SOCKET=os.environ.get('UGV_DISARM_GATEWAY_SOCKET', '/run/ugv-command/ingress.sock'),
        DISARM_GATEWAY_UID=os.environ.get('UGV_DISARM_GATEWAY_UID'),
        DISARM_FRONTEND_GID=os.environ.get('UGV_DISARM_FRONTEND_GID'))
    if config:
        app.config.update(config)
    origin = urlsplit(app.config['PUBLIC_ORIGIN'])
    if origin.scheme not in ('https', 'http') or not origin.hostname or origin.path or origin.query or origin.fragment or origin.username:
        raise RuntimeError('PUBLIC_ORIGIN must be a bare HTTPS origin')
    if origin.scheme != 'https' and (app.config['COOKIE_SECURE'] or origin.hostname not in ('localhost', '127.0.0.1', '::1')):
        raise RuntimeError('HTTP is allowed only for explicit loopback development')
    if origin.scheme == 'https' and not app.config['COOKIE_SECURE']:
        raise RuntimeError('HTTPS requires Secure cookies')
    app.config['TRUSTED_HOSTS'] = [origin.hostname]
    store = auth_store or AuthStore(app.config['AUTH_STATE_DIR'], idle=app.config['IDLE_SECONDS'],
                                   lifetime=app.config['MAX_SESSION_SECONDS'])
    sim = simulator if simulator is not None else Simulator()
    reader = telemetry if telemetry is not None else PixhawkTelemetry()
    bridge = TelemetryBridge()
    from disarm_control.ipc import OwnerClient, SocketPolicy
    from disarm_control.engine import UUID, finite
    if disarm_client is not None:
        disarm = disarm_client
    elif (app.config['DISARM_ENABLED'] or app.config['DISARM_GATEWAY_UID'] is not None or
          app.config['DISARM_FRONTEND_GID'] is not None):
        gateway_uid = app.config['DISARM_GATEWAY_UID']
        frontend_gid = app.config['DISARM_FRONTEND_GID']
        for value in (gateway_uid, frontend_gid):
            if not (type(value) is int and value > 0 or
                    type(value) is str and value.isascii() and value.isdecimal() and int(value) > 0):
                raise RuntimeError('Explicit DISARM gateway UID and frontend GID required')
        if int(gateway_uid) == os.getuid():
            raise RuntimeError('DISARM gateway requires a distinct service UID')
        disarm = OwnerClient(app.config['DISARM_GATEWAY_SOCKET'],
                             policy=SocketPolicy(int(gateway_uid), int(frontend_gid), 0o710, 0o660))
    else:
        disarm = OwnerClient(app.config['DISARM_GATEWAY_SOCKET'])
    disarm_audit_states = OrderedDict()
    disarm_audit_lock = RLock()
    statistics = MissionStatistics(store)
    sim.track_missions(statistics)
    app.extensions.update(auth_store=store, pixhawk_telemetry=reader, mission_statistics=statistics, simulator=sim)

    def cookie(response, token, age):
        response.set_cookie(COOKIE, token, max_age=age, secure=app.config['COOKIE_SECURE'],
                            httponly=True, samesite='Strict', path='/')
        return response

    def deny(message, status):
        if request.path.startswith('/api/'):
            return jsonify(error=message), status
        return render_template('error.html', message=message), status

    route_permissions = {
        'index': 'status.view', 'status': 'status.view', 'telemetry_status': 'telemetry.view',
        'audit_page': 'audit.view', 'own_audit': 'audit.view', 'all_audit': 'audit.all',
        'statistics_page': 'statistics.view', 'own_statistics': 'statistics.view',
        'all_statistics': 'statistics.all', 'statistics_operators': 'statistics.all',
        'command': 'demo.control', 'users': 'users.manage', 'create_user': 'users.manage',
        'update_user': 'users.manage', 'roles': 'roles.manage', 'create_role': 'roles.manage',
        'update_role': 'roles.manage',
        'pixhawk_disarm': 'pixhawk.disarm', 'pixhawk_disarm_status': 'pixhawk.disarm',
    }

    @app.context_processor
    def access_context():
        return {'can': lambda permission: bool(getattr(g, 'user', None) and permission in g.user['permissions'])}

    @app.before_request
    def authorize():
        if isinstance(request.routing_exception, SecurityError):
            raise request.routing_exception
        g.access_guard = None
        g.token = request.cookies.get(COOKIE)
        g.session = store.session(g.token)
        if g.session and g.session['user_id']:
            # Hold the access lock through authorization and dispatch; access mutations
            # use the same cross-process lock, while audit commits remain independent.
            g.access_guard = store.access_lock()
            g.access_guard.__enter__()
            g.session = store.session(g.token)
        g.user = (dict(id=g.session['user_id'], username=g.session['username'], role=g.session['role'],
                            rank=g.session['rank'], permissions=g.session['permissions'])
                  if g.session and g.session['user_id'] else None)
        public = request.endpoint == 'login' or (request.endpoint == 'static' and request.view_args.get('filename') == 'style.css')
        if not public and not g.user:
            if request.path.startswith('/api/'):
                return jsonify(error='Потрібен вхід.'), 401
            return redirect('/login', code=303)
        required = route_permissions.get(request.endpoint)
        if request.endpoint == 'access_page':
            allowed = g.user and set(g.user['permissions']) & {'users.manage', 'roles.manage'}
        else:
            allowed = not required or required in g.user['permissions']
            if request.path.startswith('/api/') and request.endpoint and not required and request.endpoint not in ('session_info', 'activity'):
                allowed = False
        if not allowed:
            target = 'endpoint:' + request.endpoint
            args = request.view_args or {}
            if type(args.get('target_id')) is int:
                target += ' user_id:' + str(args['target_id'])
            elif isinstance(args.get('name'), str):
                from re import fullmatch
                name = args['name']
                target += ' role:' + (name if fullmatch(r'[a-z][a-z0-9_-]{2,31}', name) else 'INVALID_TARGET')
            store.audit(g.user, 'auth.forbidden', 'DENIED', target)
            return deny('Недостатньо прав.', 403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            supplied_origin = request.headers.get('Origin')
            # Evaluate the existing policy once; never retain request values in diagnostics.
            origin_matches = not supplied_origin or supplied_origin == app.config['PUBLIC_ORIGIN']
            fetch_site_allowed = request.headers.get('Sec-Fetch-Site') != 'cross-site'
            csrf_matches = bool(g.session and isinstance(token, str) and token.isascii()
                                and len(token) <= 128 and hmac.compare_digest(token, g.session['csrf']))
            if not origin_matches or not fetch_site_allowed or not csrf_matches:
                request_id = secrets.token_hex(16)
                detail = dict(request_id=request_id, origin_matches=bool(origin_matches),
                              fetch_site_allowed=fetch_site_allowed, cookie_present=COOKIE in request.cookies,
                              session_found=g.session is not None, csrf_present=bool(token),
                              csrf_matches=csrf_matches, **{'request.is_secure': bool(request.is_secure)})
                detail['public_origin'] = diagnostic_origin(app.config['PUBLIC_ORIGIN'])
                detail['request_origin'] = diagnostic_origin(supplied_origin)
                store.audit(g.user, 'csrf.denied', 'DENIED', json.dumps(detail, sort_keys=True))
                response = app.make_response(deny('Запит відхилено. Оновіть сторінку.', 403))
                response.headers['X-Request-ID'] = request_id
                return response
        if g.user and (request.path in ('/', '/audit', '/statistics') or request.endpoint in ('command', 'activity', 'pixhawk_disarm')):
            store.touch(g.token)

    @app.after_request
    def headers(response):
        context = getattr(g, 'access_guard', None)
        if context is not None:
            g.access_guard = None
            context.__exit__(None, None, None)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Vary'] = 'Cookie'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        nonce = getattr(g, 'login_script_nonce', None)
        if nonce:
            response.headers['Content-Security-Policy'] += "; script-src 'self' 'nonce-" + nonce + "'"
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.teardown_request
    def release_access_lock(exc):
        context = getattr(g, 'access_guard', None)
        if context is not None:
            g.access_guard = None
            error = exc or RuntimeError('request interrupted')
            context.__exit__(type(error), error, error.__traceback__)

    @app.errorhandler(AccountError)
    def account_error(exc):
        return deny(str(exc), 403 if isinstance(exc, AccessDenied) else 400)

    @app.errorhandler(sqlite3.Error)
    @app.errorhandler(OSError)
    @app.errorhandler(RuntimeError)
    def storage_error(exc):
        # No raw exception/request data, tokens, SQL parameters or credentials in logs.
        app.logger.error('HMI private storage operation failed')
        return deny('Сховище HMI недоступне. Перевірте стан перед повторенням дії.', 503)

    def landing(user):
        permissions = user.get('permissions', []) if user else []
        for permission, path in (('status.view','/'), ('users.manage','/access'), ('roles.manage','/access'),
                                 ('audit.view','/audit'), ('statistics.view','/statistics')):
            if permission in permissions:
                return path
        return '/account'

    @app.get('/account')
    def account_page():
        return render_template('profile.html', user=g.user, csrf=g.session['csrf'])

    def render_login(csrf, error):
        # Permit only this response's password reveal script, without unsafe-inline
        # or changes to the public asset/authentication allowlist.
        g.login_script_nonce = secrets.token_urlsafe(24)
        return render_template('login.html', csrf=csrf, error=error,
                               login_script_nonce=g.login_script_nonce)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'GET':
            if g.user:
                return redirect(landing(g.user), code=303)
            token = g.token if g.session else store.anonymous_session()
            session = store.session(token)
            return cookie(app.make_response(render_login(session['csrf'], None)), token, 300)
        token, status = store.login(request.form.get('username', ''), request.form.get('password', ''), request.remote_addr, g.token)
        if token:
            session = store.session(token)
            return cookie(redirect(landing(session), code=303), token, store.lifetime)
        return render_login(g.session['csrf'], LOGIN_ERROR), status

    @app.post('/logout')
    def logout():
        store.logout(g.token, g.user)
        response = redirect('/login', code=303)
        response.delete_cookie(COOKIE, path='/', secure=app.config['COOKIE_SECURE'], httponly=True, samesite='Strict')
        return response

    @app.get('/')
    def index():
        return render_template('index.html', user=g.user, csrf=g.session['csrf'],
                               disarm_enabled=app.config['DISARM_ENABLED'])

    @app.get('/audit')
    def audit_page():
        return render_template('audit.html', user=g.user, csrf=g.session['csrf'])

    @app.get('/statistics')
    def statistics_page():
        return render_template('statistics.html', user=g.user, csrf=g.session['csrf'])

    def mission_report(all_users=False):
        period = request.args.get('period', 'day')
        if period not in PERIODS:
            return jsonify(error='Невідомий період.'), 400
        requested = request.args.get('operator_id')
        if requested is not None and (not requested.isascii() or not requested.isdigit() or len(requested)>18 or int(requested)<=0):
            return jsonify(error='Некоректний оператор.'), 400
        operator_id = int(requested) if requested is not None else None
        if not all_users:
            if (operator_id is not None and operator_id != g.user['id']) or request.args.get('scope') == 'all':
                store.audit(g.user, 'auth.forbidden', 'DENIED')
                return jsonify(error='Недостатньо прав.'), 403
            operator_id = g.user['id']
        # Reconcile natural DEMO completion even if this is the first poll since START.
        sim.snapshot()
        try:
            report = statistics.report(period, operator_id=operator_id, by_operator=all_users)
        except LookupError:
            return jsonify(error='Оператор не знайдений.'), 404
        report['scope'] = ('operator' if operator_id is not None else 'all') if all_users else 'self'
        return jsonify(report)

    @app.get('/api/statistics')
    def own_statistics():
        return mission_report()

    @app.get('/api/admin/statistics')
    def all_statistics():
        return mission_report(True)

    @app.get('/api/session')
    def session_info():
        return jsonify(user=g.user)

    @app.post('/api/session/activity')
    def activity():
        return jsonify(ok=True)

    def audit_entries(all_users=False):
        value = request.args.get('before')
        if value is not None and (not value.isascii() or not value.isdigit() or len(value) > 18):
            return jsonify(error='Некоректний курсор.'), 400
        entries = store.logs(None if all_users else g.user, int(value) if value else None)
        return jsonify(entries=entries, next_before=entries[-1]['id'] if len(entries) == 100 else None)

    @app.get('/api/audit')
    def own_audit():
        return audit_entries()

    @app.get('/api/admin/audit')
    def all_audit():
        return audit_entries(True)

    @app.get('/api/admin/users')
    def users():
        return jsonify(users=store.users())

    @app.get('/access')
    def access_page():
        role_list = store.roles()
        assignable = [role for role in role_list if g.user['role'] == 'superadmin' or (
            role['rank'] < g.user['rank'] and set(role['permissions']) <= set(g.user['permissions']))]
        return render_template('access.html', user=g.user, csrf=g.session['csrf'],
                               permissions=PERMISSIONS, roles=role_list, role_map={r['name']: r for r in role_list},
                               assignable_roles=sorted(assignable, key=lambda r: r['rank']), users=store.users() if 'users.manage' in g.user['permissions'] else [])

    @app.get('/api/statistics/operators')
    def statistics_operators():
        return jsonify(users=store.users(include_deleted=True))

    def access_body():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise AccountError('Очікується JSON-об’єкт.')
        return body

    @app.post('/api/admin/users')
    def create_user():
        store.manage_user(g.token, 'create', access_body())
        return jsonify(ok=True, message='Користувача створено.'), 201

    @app.post('/api/admin/users/<int:target_id>')
    def update_user(target_id):
        body = access_body()
        action = body.pop('action', None)
        if not isinstance(action, str):
            raise AccountError('Очікується назва операції.')
        store.manage_user(g.token, action, body, target_id)
        return jsonify(ok=True, message='Зміни користувача збережено. Попередні сесії відкликано.')

    @app.get('/api/admin/roles')
    def roles():
        return jsonify(roles=store.roles(), permissions=PERMISSIONS)

    @app.post('/api/admin/roles')
    def create_role():
        body = access_body()
        name = body.pop('name', None)
        store.manage_role(g.token, 'create', name, body)
        return jsonify(ok=True, message='Роль створено.'), 201

    @app.post('/api/admin/roles/<name>')
    def update_role(name):
        body = access_body()
        action = body.pop('action', None)
        if not isinstance(action, str):
            raise AccountError('Очікується назва операції.')
        store.manage_role(g.token, action, name, body)
        return jsonify(ok=True, message='Зміни ролі збережено. Попередні сесії її користувачів відкликано.')

    @app.get('/api/telemetry')
    def telemetry_status():
        return jsonify(telemetry.snapshot() if telemetry is not None else bridge.snapshot())

    def visible(snapshot):
        if 'audit.all' not in g.user['permissions']:
            snapshot = dict(snapshot, events=[])
        return snapshot

    @app.get('/api/status')
    def status():
        return jsonify(visible(sim.snapshot()))

    @app.post('/api/command')
    def command():
        body = request.get_json(silent=True)
        action = body.get('action') if isinstance(body, dict) else None
        label = 'demo.' + (action if isinstance(action, str) and action in DEMO_ACTIONS else 'invalid')
        # Persist intent before touching DEMO; a final audit failure leaves PENDING.
        event = store.audit(g.user, label, 'PENDING')
        if not isinstance(body, dict):
            store.finish_event(event, 'INVALID')
            return jsonify(error='Очікується JSON-об’єкт.'), 400
        try:
            result = sim.command(body, actor=g.user)
        except TransitionError as exc:
            store.finish_event(event, 'REJECTED')
            return jsonify(error=str(exc), status=visible(sim.snapshot())), 409
        store.finish_event(event, 'OK')
        return jsonify(visible(result))

    def disarm_request(op, request_id):
        if type(request_id) is not str or not UUID.fullmatch(request_id):
            return jsonify(error='Некоректний ідентифікатор запиту.'), 400
        # Feature OFF disables new actions; existing UUIDs remain observable.
        if op == 'DISARM' and not app.config['DISARM_ENABLED']:
            return jsonify(error='DISARM вимкнено; нові запити не приймаються.'), 503
        event = None
        if op == 'DISARM':
            event = store.audit(g.user, 'pixhawk.disarm', 'PENDING', request_id)
        try:
            clock = app.config.get('DISARM_CLOCK', time.monotonic)
            started = clock()
            if not finite(started):
                raise ValueError('Invalid clock')
            result = disarm.call(op, g.user, request_id)
            received = clock()
            if not finite(received) or received < started:
                raise ValueError('Invalid clock')
        except (OSError, ValueError, RuntimeError):
            if event is not None:
                store.finish_event(event, 'DELIVERY_UNKNOWN')
            return jsonify(error='Командний gateway недоступний. Доставка і стан UNKNOWN; автоматичного повтору немає.',
                           delivery_state='UNKNOWN', observation_state='UNKNOWN', command_outcome='UNKNOWN'), 503
        if type(result) is not dict or type(result.get('ok')) is not bool:
            if event is not None:
                store.finish_event(event, 'DELIVERY_UNKNOWN')
            return jsonify(error='Недостовірна відповідь gateway.', observation_state='UNKNOWN'), 503
        if not result.get('ok'):
            uncertain = op == 'STATUS' or result.get('error') in (
                'BACKEND_RESULT_UNKNOWN', 'INVALID_BACKEND_RESPONSE',
                'OWNER_UNAVAILABLE', 'AUDIT_STORAGE_UNAVAILABLE')
            if event is not None:
                store.finish_event(event, 'DELIVERY_UNKNOWN' if uncertain else 'REJECTED')
            return jsonify(error=result.get('error', 'GATEWAY_UNAVAILABLE'),
                           delivery_state='UNKNOWN' if uncertain else 'REJECTED',
                           observation_state='UNKNOWN', command_outcome='UNKNOWN'), 503 if uncertain else 409
        command = result.get('command', {})
        valid = (type(command) is dict and command.get('id') == request_id and
                 type(command.get('actor_id')) is int and command['actor_id'] == g.user['id'] and
                 command.get('state') == command.get('delivery_state') and
                 command.get('delivery_state') in ('QUEUED', 'SENT', 'UNKNOWN', 'REJECTED') and
                 command.get('observation_state') in ('DISARMED_OBSERVED', 'ARMED_OBSERVED', 'UNKNOWN') and
                 command.get('command_outcome') == 'UNKNOWN' and
                 type(command.get('observation_pending')) is bool and
                 finite(command.get('heartbeat_freshness_s')) and 0 < command['heartbeat_freshness_s'] <= 3)
        if valid and command['observation_state'] != 'UNKNOWN':
            source = command.get('heartbeat_source')
            valid = (command['delivery_state'] == 'SENT' and
                     finite(command.get('heartbeat_age_s')) and
                     0 <= command['heartbeat_age_s'] < command['heartbeat_freshness_s'] and
                     finite(command.get('heartbeat_observed_at')) and
                     type(command.get('heartbeat_rx_serial')) is int and command['heartbeat_rx_serial'] > 0 and
                     type(source) in (list, tuple) and len(source) == 2 and
                     type(source[0]) is int and 1 <= source[0] <= 255 and
                     type(source[1]) is int and source[1] == 1)
        if valid and command['observation_state'] == 'UNKNOWN':
            valid = all(command.get(key) is None for key in
                        ('heartbeat_age_s', 'heartbeat_rx_serial', 'heartbeat_observed_at', 'heartbeat_source'))
        if not valid:
            if event is not None:
                store.finish_event(event, 'DELIVERY_UNKNOWN')
            return jsonify(error='Недостовірна відповідь gateway; стан UNKNOWN.', observation_state='UNKNOWN'), 503
        command = dict(command)
        if command['observation_state'] != 'UNKNOWN':
            current_age = max(command['heartbeat_age_s'] + received - started,
                              received - command['heartbeat_observed_at'])
            if received < command['heartbeat_observed_at']:
                if event is not None:
                    store.finish_event(event, 'DELIVERY_UNKNOWN')
                return jsonify(error='Майбутня часова мітка; стан UNKNOWN.', observation_state='UNKNOWN'), 503
            if not finite(current_age) or current_age >= command['heartbeat_freshness_s']:
                command.update(observation_state='UNKNOWN', observation_pending=False,
                               observation_reason='STALE_DURING_HMI_RELAY', heartbeat_age_s=None,
                               heartbeat_rx_serial=None, heartbeat_observed_at=None, heartbeat_source=None)
            else:
                command['heartbeat_age_s'] = current_age
        if event is not None:
            store.finish_event(event, 'ACCEPTED_BY_GATEWAY')
        # Audit transitions, not every status poll/age change. The authoritative
        # owner journal persists delivery state even when the browser is closed.
        key = (g.user['id'], request_id)
        fingerprint = (command['delivery_state'], command['observation_state'], command.get('observation_reason'))
        with disarm_audit_lock:
            if disarm_audit_states.get(key) != fingerprint:
                store.audit(g.user, 'pixhawk.disarm.observation', command['observation_state'],
                            json.dumps(dict(request_id=request_id, delivery_state=command['delivery_state'],
                                            command_outcome='UNKNOWN', reason=command.get('observation_reason'))))
                disarm_audit_states[key] = fingerprint
                disarm_audit_states.move_to_end(key)
                while len(disarm_audit_states) > 256:
                    disarm_audit_states.popitem(last=False)
        return jsonify(command)

    @app.post('/api/pixhawk/disarm')
    def pixhawk_disarm():
        body = request.get_json(silent=True)
        if type(body) is not dict or set(body) != {'request_id'}:
            return jsonify(error='Потрібен лише request_id; ARM і force-параметри заборонені.'), 400
        return disarm_request('DISARM', body['request_id'])

    @app.get('/api/pixhawk/disarm/<request_id>')
    def pixhawk_disarm_status(request_id):
        return disarm_request('STATUS', request_id)

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1'])
    parser.add_argument('--port', type=int, default=8082)
    parser.add_argument('--http-loopback', action='store_true', help='Explicit loopback-only development HTTP')
    parser.add_argument('--telemetry', action='store_true')
    parser.add_argument('--telemetry-owner-helper', action='store_true')
    args = parser.parse_args()
    if args.telemetry != args.telemetry_owner_helper:
        parser.error('Telemetry requires both --telemetry and --telemetry-owner-helper')
    app = create_app(config={'PUBLIC_ORIGIN': f'http://127.0.0.1:{args.port}', 'COOKIE_SECURE': False} if args.http_loopback else None)
    try:
        from .runtime import start_hmi, start_telemetry, stop_telemetry
    except ImportError:
        from runtime import start_hmi, start_telemetry, stop_telemetry
    start_hmi(app)
    atexit.register(stop_telemetry, app)
    if args.telemetry:
        start_telemetry(app)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
