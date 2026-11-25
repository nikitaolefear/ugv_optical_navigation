"""Reviewed deployment template. Not installed or started by tests."""
import os

bind = '127.0.0.1:8083'
forwarded_allow_ips = '127.0.0.1'
workers = 1
worker_class = 'gthread'
threads = 4
preload_app = False
reload = False
max_requests = 0
accesslog = None  # Audit is in SQLite; do not log query strings or request headers.
errorlog = '-'
timeout = 30
graceful_timeout = 10
umask = 0o077


def on_starting(server):
    if server.cfg.workers != 1 or server.cfg.preload_app or server.cfg.reload:
        raise RuntimeError('UGV requires one worker, no preload/reloader')


def post_worker_init(worker):
    from web_hmi.runtime import start_hmi, start_telemetry
    app = worker.wsgi
    start_hmi(app)
    from web_hmi.technical_telemetry_log import start_technical_log
    start_technical_log(app)
    if os.environ.get('UGV_HMI_TELEMETRY') == '1':
        start_telemetry(app)


def worker_exit(server, worker):
    if getattr(worker, 'wsgi', None):
        from web_hmi.technical_telemetry_log import stop_technical_log
        stop_technical_log(worker.wsgi)
        from web_hmi.runtime import stop_telemetry
        stop_telemetry(worker.wsgi)
