"""Explicit bench replacement entry point for 8082's sole serial owner.
No hardware access at import. Do not run alongside ugv-hmi.service.
"""
import argparse
import atexit
import threading
from functools import partial
from werkzeug.serving import make_server
from web_hmi.app import create_app as telemetry_app
from web_hmi.pixhawk_telemetry import PixhawkTelemetry
from web_hmi.pixhawk_owner_client import open_with_owner_helper
from stop_latch_test.hold_bench import HoldBench, hold_sender
from stop_latch_test.stop_latch import monitor
from stop_latch_test.server import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--enable-bench-hold', action='store_true', required=True)
    args = parser.parse_args()
    reader = PixhawkTelemetry(opener=partial(open_with_owner_helper, bench_write=True))
    bench = HoldBench(args.journal, reader, hold_sender())
    reader.bench_tick = bench.tick
    stop = threading.Event()
    gpio = threading.Thread(target=monitor, args=(bench, stop), daemon=True)
    page = make_server('127.0.0.1', 8084, create_app(bench, hold=True), threaded=True)
    threading.Thread(target=page.serve_forever, daemon=True).start()
    gpio.start()
    atexit.register(stop.set)
    atexit.register(reader.close)
    atexit.register(page.shutdown)
    reader.start()
    telemetry_app(telemetry=reader).run(host='127.0.0.1', port=8082, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
