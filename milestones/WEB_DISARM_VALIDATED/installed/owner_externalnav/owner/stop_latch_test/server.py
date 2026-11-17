"""Read-only legacy diagnostics; control is exclusively through the gateway."""
import argparse
import atexit
import threading
from pathlib import Path
from flask import Flask, jsonify, request
try:
    from .stop_latch import StopLatch, Rejected, monitor
except ImportError:
    from stop_latch import StopLatch, Rejected, monitor

def create_app(latch, *, hold=False):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=1024, TRUSTED_HOSTS=['127.0.0.1','localhost'])
    @app.before_request
    def guard():
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            return jsonify(error='LEGACY_CONTROL_DISABLED',
                           detail='8084 доступний лише для читання. Керування через авторизований HMI.'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get('/')
    def index():
        return ('<!doctype html><html lang="uk"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>UGV · діагностика</title><h1>Діагностика лише для читання</h1>'
                '<p>Керувальні маршрути 8084 вимкнено. RESET і HOLD-тест тут недоступні.</p>'
                '<p><a href="/api/state">Переглянути поточний стан</a></p></html>')

    @app.get('/api/state')
    def state():
        result = dict(latch.snapshot())
        result.pop('csrf', None)
        result['legacy_control'] = 'DISABLED'
        return jsonify(result)

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
