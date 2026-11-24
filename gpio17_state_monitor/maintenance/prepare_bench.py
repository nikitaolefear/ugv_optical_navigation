"""Assemble inert bench releases and capture readable production backups."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BASES = {
    'frontend_gateway': Path('/opt/ugv-disarm/package'),
    'owner_externalnav': Path('/opt/ugv-tilted-observe/20261117/package'),
}
DBS = [Path('/var/lib/ugv-pixhawk/disarm.sqlite3'), Path('/var/lib/ugv-hmi/auth.sqlite3')]
UNITS = ('ugv-hmi.service', 'ugv-command-gateway.service', 'ugv-hmi-auth.service')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output):
    pins = json.loads((ROOT / 'base-sha256.json').read_text())
    for name, base in BASES.items():
        for relative, expected in pins[name].items():
            if digest(base / relative) != expected:
                raise ValueError(f'Installed base differs: {base / relative}')
        tree = output / name
        shutil.copytree(base, tree)
        shutil.copytree(ROOT / name, tree, dirs_exist_ok=True)
        with tarfile.open(output / f'{name}.tar.gz', 'w:gz') as archive:
            archive.add(tree, arcname='package')
        shutil.rmtree(tree)
    shutil.copyfile(ROOT / 'pixhawk/ugv_brake.lua', output / 'ugv_brake.lua')
    shutil.copyfile(ROOT / 'maintenance/fetch_pixhawk_export.py', output / 'fetch_pixhawk_export.py')
    shutil.copyfile(ROOT / 'maintenance/pixhawk-export-readonly.patch', output / 'pixhawk-export-readonly.patch')
    artifacts = sorted(path for path in output.iterdir() if path.is_file())
    (output / 'SHA256SUMS').write_text(''.join(f'{digest(path)}  {path.name}\n' for path in artifacts))


def capture(output):
    target = output / 'backup'
    target.mkdir(mode=0o700)
    unavailable = []
    for unit in UNITS:
        result = subprocess.run(['systemctl', 'cat', unit], capture_output=True, text=True)
        if result.returncode:
            unavailable.append(unit)
            continue
        (target / f'{unit}.txt').write_text(result.stdout)
    for path in DBS:
        try:
            source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        except sqlite3.Error:
            unavailable.append(str(path))
            continue
        with source, sqlite3.connect(target / path.name) as backup:
            source.backup(backup)
            if backup.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError(f'Backup integrity failed: {path}')
        source.close()
    (target / 'PENDING.json').write_text(json.dumps({
        'unavailable': unavailable,
        'operator_capture_required': ['current Pixhawk full parameter export',
                                      'Pixhawk SD/APM/scripts copy and firmware ID'],
    }, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New private directory for inert artifacts')
    output = parser.parse_args().output
    output.mkdir(mode=0o700)
    build(output)
    capture(output)
    print(output)


if __name__ == '__main__':
    main()
