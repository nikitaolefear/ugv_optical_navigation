"""Save a complete read-only export from the sole owner's loopback GET."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import build_opener, ProxyHandler

URL = 'http://127.0.0.1:8084/api/pixhawk-params-export'


def fetch():
    with build_opener(ProxyHandler({})).open(URL, timeout=4) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Export exceeds size limit')
    snapshot = json.loads(raw)
    rows = snapshot['parameters']
    if snapshot['state'] != 'COMPLETE' or not rows or len(rows) != snapshot['expected']:
        raise ValueError(f"Export {snapshot['state']}: {snapshot['received']}/{snapshot['expected']}")
    for row in rows:
        if not re.fullmatch(r'[A-Za-z0-9_]{1,16}', row['name']):
            raise ValueError('Invalid parameter name')
    return ''.join(f"{row['name']},{row['value']:.9g}\n" for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='New .parm file in a private backup directory')
    destination = parser.parse_args().destination
    temporary = None
    try:
        content = fetch()
        with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)  # Existing backup must not be overwritten.
        fd = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        print(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
