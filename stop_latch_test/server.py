"""Loopback-only test page. Never imports production HMI or Pixhawk modules."""
import argparse
import atexit
import secrets
import threading
from pathlib import Path
from flask import Flask, jsonify, request, send_file
from stop_latch import StopLatch, Rejected, monitor

def create_app(latch):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=1024, TRUSTED_HOSTS=['127.0.0.1','localhost'])
    csrf = secrets.token_urlsafe(32)

    @app.before_request
    def guard():
        if request.method == 'POST':
            origin = request.headers.get('Origin')
            if (request.headers.get('X-Test-CSRF') != csrf or
                (origin is not None and origin != request.host_url.rstrip('/')) or
                request.headers.get('Sec-Fetch-Site') == 'cross-site'):
                return jsonify(error='Test request rejected'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get('/')
    def index():
        return send_file(Path(__file__).with_name('index.html'))

    @app.get('/app.js')
    def javascript():
        return send_file(Path(__file__).with_name('app.js'))

    @app.get('/api/state')
    def state():
        return jsonify(**latch.snapshot(), csrf=csrf)

    @app.post('/api/test-action')
    def action():
        body=request.get_json(silent=True)
        try:
            if not isinstance(body,dict) or not isinstance(body.get('action'),str):
                return jsonify(error='Expected test action'),400
            return jsonify(latch.command(body['action']))
        except Rejected as exc:
            return jsonify(error=str(exc), **latch.snapshot()),409
        except OSError:
            return jsonify(error='Journal unavailable', **latch.snapshot()),503

    return app

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',type=Path,required=True)
    args=parser.parse_args()
    latch=StopLatch(args.journal)
    stop=threading.Event()
    thread=threading.Thread(target=monitor,args=(latch,stop),daemon=True)
    thread.start()
    atexit.register(stop.set)
    create_app(latch).run(host='127.0.0.1',port=8084,debug=False,use_reloader=False,threaded=True)
