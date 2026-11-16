"""Factory: explicit state initialization, no telemetry start on import/factory."""
import os
from werkzeug.middleware.proxy_fix import ProxyFix
from .app import create_app


def create_wsgi_app():
    app = create_app()
    # Only the loopback reverse proxy may reach this listener. Never trust X-Forwarded-Host.
    if os.environ.get('UGV_HMI_TRUST_PROXY') == '1':
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)
    return app
