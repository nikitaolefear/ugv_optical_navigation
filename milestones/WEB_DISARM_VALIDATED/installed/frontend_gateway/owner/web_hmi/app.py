"""HMI v0.3: DEMO simulator and independent read-only telemetry."""
import argparse
import atexit
from flask import Flask, jsonify, render_template, request
try:
    from .simulator import Simulator, TransitionError
    from .pixhawk_telemetry import PixhawkTelemetry
except ImportError:
    from simulator import Simulator, TransitionError
    from pixhawk_telemetry import PixhawkTelemetry


def create_app(simulator=None, telemetry=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 4096
    sim = simulator if simulator is not None else Simulator()

    reader = telemetry if telemetry is not None else PixhawkTelemetry()
    app.extensions['pixhawk_telemetry'] = reader

    @app.get('/api/telemetry')
    def telemetry_status():
        return jsonify(reader.snapshot())

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/status')
    def status():
        return jsonify(sim.snapshot())

    @app.post('/api/command')
    def command():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error='Очікується JSON-об’єкт.'), 400
        try:
            return jsonify(sim.command(body))
        except TransitionError as exc:
            return jsonify(error=str(exc), status=sim.snapshot()), 409

    return app


app = create_app()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--telemetry', action='store_true', help='Enable passive serial reading; never sends MAVLink')
    parser.add_argument('--telemetry-owner-helper', action='store_true',
                        help='Use the fixed privileged ownership checker; Flask stays unprivileged')
    args = parser.parse_args()
    if args.telemetry_owner_helper and not args.telemetry:
        parser.error('--telemetry-owner-helper requires --telemetry')
    if args.telemetry:
        reader = app.extensions['pixhawk_telemetry']
        if args.telemetry_owner_helper:
            try:
                from .pixhawk_owner_client import open_with_owner_helper
            except ImportError:
                from pixhawk_owner_client import open_with_owner_helper
            reader.opener = open_with_owner_helper
        reader.start()
        atexit.register(reader.close)
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
