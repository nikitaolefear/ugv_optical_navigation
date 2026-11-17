"""Explicit worker lifecycle. Importing Flask/WSGI factory never opens hardware."""
import fcntl
import os
import threading

try:
    from .auth import private_file
    from .pixhawk_owner_client import open_with_owner_helper
except ImportError:
    from auth import private_file
    from pixhawk_owner_client import open_with_owner_helper


def start_hmi(app):
    """Own the DEMO engine before recovering prior process missions, even without telemetry."""
    if 'engine_lock' in app.extensions:
        return
    store = app.extensions['auth_store']
    path = store.directory / 'hmi-engine.lock'
    private_file(path)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app.extensions['mission_statistics'].recover_demo()
        store.audit(None, 'hmi.started', 'OK')
    except BaseException:
        os.close(fd)
        raise
    app.extensions['engine_lock'] = fd


def start_telemetry(app):
    if app.config.get('DISARM_ENABLED'):
        raise RuntimeError('DISARM HMI uses the existing serial owner; local reader forbidden')
    if 'reader_lock' in app.extensions:
        return
    store = app.extensions['auth_store']
    path = store.directory / 'serial-reader.lock'
    private_file(path)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.audit(None, 'telemetry.started', 'OK')
        reader = app.extensions['pixhawk_telemetry']
        reader.opener = open_with_owner_helper
        reader.start()
    except BaseException:
        os.close(fd)
        raise
    app.extensions['reader_lock'] = fd
    stop = threading.Event()
    app.extensions['monitor_stop'] = stop
    def monitor():
        previous = None
        while not stop.is_set():
            try:
                state = reader.snapshot()['connection']
                if state != previous:
                    # Snapshot connection is an internal fixed enum, not message contents.
                    store.audit(None, 'telemetry.state', 'OK', state)
                    previous = state
            except Exception:
                app.logger.error('HMI telemetry audit unavailable')
            stop.wait(1)
    monitor_thread = threading.Thread(target=monitor, name='hmi-audit', daemon=True)
    app.extensions['monitor_thread'] = monitor_thread
    monitor_thread.start()


def stop_telemetry(app):
    stop = app.extensions.get('monitor_stop')
    if stop:
        stop.set()
        app.extensions['monitor_thread'].join(timeout=2)
    reader = app.extensions['pixhawk_telemetry']
    reader.close()
    # Never release the inter-process guard while a reader thread is still alive.
    if not reader.thread or not reader.thread.is_alive():
        fd = app.extensions.pop('reader_lock', None)
        if fd is not None:
            os.close(fd)
    alive = reader.thread and reader.thread.is_alive()
    if not alive and 'engine_lock' in app.extensions:
        app.extensions['simulator'].snapshot()
        app.extensions['mission_statistics'].recover_demo()
        os.close(app.extensions.pop('engine_lock'))
    app.extensions['auth_store'].audit(None, 'hmi.stop_requested' if alive else 'hmi.stopped',
                                       'PENDING' if alive else 'OK')
