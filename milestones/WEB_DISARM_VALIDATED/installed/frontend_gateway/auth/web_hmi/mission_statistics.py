"""Durable mission accounting, independent of bounded security audit.

Calendar windows are Europe/Kyiv: starts use START time, results use END time,
active is a current-state gauge. All attribution stays with the START principal. REAL is unavailable without a future trusted mission source.
No telemetry imports or vehicle commands.
"""
from contextlib import contextmanager
from datetime import datetime, time, timedelta
import fcntl
import os
import math
import uuid
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Kyiv')
PERIODS = ('hour', 'day', 'week', 'month', 'year', 'all_time')
COUNTERS = ('started', 'completed', 'cancelled', 'failed', 'interrupted')
try:
    from .auth import private_file
except ImportError:
    from auth import private_file

OUTCOMES = {'COMPLETED':'completed', 'STOPPED':'cancelled', 'RESET':'cancelled',
            'HOLD':'cancelled', 'FAULT':'failed', 'E_STOP':'failed', 'INTERRUPTED':'interrupted'}


def window(period, now):
    if period not in PERIODS:
        raise ValueError('Unknown statistics period')
    local = datetime.fromtimestamp(now, TZ)
    if period == 'all_time':
        return None, None
    if period == 'hour':
        start = local.replace(minute=0, second=0, microsecond=0).timestamp()
        # UTC arithmetic keeps the two repeated autumn hours distinct.
        return start, start + 3600
    today = local.date()
    if period == 'day':
        first, after = today, today + timedelta(days=1)
    elif period == 'week':
        first = today - timedelta(days=today.weekday())
        after = first + timedelta(days=7)
    elif period == 'month':
        first = today.replace(day=1)
        after = first.replace(year=first.year+1, month=1) if first.month == 12 else first.replace(month=first.month+1)
    else:
        first, after = today.replace(month=1, day=1), today.replace(year=today.year+1, month=1, day=1)
    return (datetime.combine(first, time.min, TZ).timestamp(), datetime.combine(after, time.min, TZ).timestamp())


def metrics(values=None, *, active=0, results_complete=True):
    result = {name: int(values[name]) if values else 0 for name in COUNTERS}
    if any(value < 0 for value in result.values()) or active < 0:
        raise RuntimeError('Mission counter integrity error')
    # Completions can exceed starts in a period: they are separate event times.
    result['active'] = active
    result['results_complete'] = results_complete
    finished = sum(result[name] for name in COUNTERS[1:])
    result['success_rate_pct'] = round(100*result['completed']/finished, 2) if finished and results_complete else None
    return result


class MissionStatistics:
    def __init__(self, store, *, real_connected=False):
        self.store, self.real_connected = store, real_connected
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS mission_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS mission_runs(
                id TEXT PRIMARY KEY, domain TEXT NOT NULL CHECK(domain IN ('DEMO','REAL')),
                operator_id INTEGER NOT NULL REFERENCES users(id), operator_name TEXT NOT NULL,
                operator_role TEXT NOT NULL, route INTEGER NOT NULL, started REAL NOT NULL,
                ended REAL, outcome TEXT NOT NULL CHECK(outcome IN
                ('ACTIVE','COMPLETED','STOPPED','RESET','HOLD','FAULT','E_STOP','INTERRUPTED')))''')
            db.execute('''CREATE TABLE IF NOT EXISTS mission_counts(
                domain TEXT NOT NULL CHECK(domain IN ('DEMO','REAL')), operator_id INTEGER NOT NULL REFERENCES users(id),
                hour INTEGER NOT NULL, started INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
                interrupted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(domain,operator_id,hour))''')
            db.execute('''CREATE TABLE IF NOT EXISTS mission_history_gaps(
                domain TEXT NOT NULL, operator_id INTEGER NOT NULL REFERENCES users(id),
                completed INTEGER NOT NULL, cancelled INTEGER NOT NULL, failed INTEGER NOT NULL,
                interrupted INTEGER NOT NULL, PRIMARY KEY(domain,operator_id))''')
            version = db.execute("SELECT value FROM mission_meta WHERE key='version'").fetchone()
            if version is None:
                if db.execute('SELECT 1 FROM mission_counts LIMIT 1').fetchone() or db.execute('SELECT 1 FROM mission_runs LIMIT 1').fetchone():
                    raise RuntimeError('Mission schema version is missing; refusing to infer semantics')
                db.execute("INSERT INTO mission_meta VALUES('version','2')")
            elif version[0] == '1':
                with self._migration_guard():
                    self._migrate_v1(db)
            elif version[0] != '2':
                raise RuntimeError('Unsupported mission schema')
            for domain in ('DEMO', 'REAL') if real_connected else ('DEMO',):
                db.execute('INSERT OR IGNORE INTO mission_meta VALUES(?,?)',
                           ('coverage_'+domain, str(store.clock())))

    @contextmanager
    def _migration_guard(self):
        # The supported v1 service holds this same lock for its whole lifetime.
        path = self.store.directory / 'hmi-engine.lock'
        private_file(path)
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('Stop the existing HMI before migrating mission schema') from exc
            yield
        finally:
            os.close(fd)

    def _migrate_v1(self, db):
        """One transaction: never increment all-time and never infer a missing END."""
        cutover = self.store.clock()
        if not math.isfinite(cutover):
            raise RuntimeError('Invalid migration clock')
        totals = {(r['domain'], r['operator_id']): dict(r) for r in db.execute('SELECT * FROM mission_counts WHERE hour=-1')}
        retained, active, ends = {}, {}, []
        for row in db.execute('SELECT * FROM mission_runs'):
            key = (row['domain'], row['operator_id'])
            if key not in totals:
                raise RuntimeError('Missing legacy all-time row; migration rolled back')
            if row['outcome'] == 'ACTIVE':
                active[key] = active.get(key, 0) + 1
                continue
            column = OUTCOMES.get(row['outcome'])
            if column is None:
                raise RuntimeError('Invalid legacy outcome')
            ended = row['ended']
            if isinstance(ended, (int, float)) and math.isfinite(ended):
                values = retained.setdefault(key, {name:0 for name in COUNTERS[1:]})
                values[column] += 1
                ends.append((key, column, int(ended//3600)*3600))
        for key, total in totals.items():
            if any(type(total[name]) is not int or total[name] < 0 for name in COUNTERS):
                raise RuntimeError('Invalid legacy counters')
            if total['started'] != sum(total[name] for name in COUNTERS[1:]) + active.get(key, 0):
                raise RuntimeError('Legacy active/all-time mismatch; migration rolled back')
            gaps = {name:total[name]-retained.get(key, {}).get(name, 0) for name in COUNTERS[1:]}
            if any(value < 0 for value in gaps.values()):
                raise RuntimeError('Legacy ledger exceeds all-time counters; migration rolled back')
            if any(gaps.values()):
                db.execute('INSERT INTO mission_history_gaps VALUES(?,?,?,?,?,?)',
                           (*key, gaps['completed'], gaps['cancelled'], gaps['failed'], gaps['interrupted']))
        # Keep legacy START hours and every all-time value. Only result hours change.
        db.execute('UPDATE mission_counts SET completed=0,cancelled=0,failed=0,interrupted=0 WHERE hour!=-1')
        for (domain, owner), column, hour in ends:
            db.execute('INSERT INTO mission_counts(domain,operator_id,hour,'+column+') VALUES(?,?,?,1) '
                       'ON CONFLICT(domain,operator_id,hour) DO UPDATE SET '+column+'='+column+'+1', (domain,owner,hour))
        db.execute('DELETE FROM mission_counts WHERE hour!=-1 AND hour<?', (int(cutover//3600)*3600-400*86400,))
        db.execute("INSERT INTO mission_meta VALUES('result_time_migration_at',?)", (str(cutover),))
        db.execute("UPDATE mission_meta SET value='2' WHERE key='version'")

    def _prune(self, db):
        now = self.store.clock()
        # 400 days retains every current calendar-year window, including leap years.
        db.execute('DELETE FROM mission_counts WHERE hour != -1 AND hour < ?', (int(now//3600)*3600-400*86400,))
        db.execute("DELETE FROM mission_runs WHERE outcome!='ACTIVE' AND ended < ?", (now-90*86400,))
        db.execute("DELETE FROM mission_runs WHERE outcome!='ACTIVE' AND id NOT IN "
                   "(SELECT id FROM mission_runs WHERE outcome!='ACTIVE' ORDER BY ended DESC,id DESC LIMIT 10000)")

    def start_demo(self, actor, route):
        return self.start(actor, route, domain='DEMO')

    def start(self, actor, route, *, domain):
        if domain not in ('DEMO','REAL') or (domain == 'REAL' and not self.real_connected):
            raise RuntimeError('Real mission source is not connected')
        if not actor or type(actor.get('id')) is not int or route not in (1,2):
            raise RuntimeError('Mission START requires a known initiator and route')
        now, run_id = self.store.clock(), str(uuid.uuid4())
        if not math.isfinite(now):
            raise RuntimeError('Invalid mission clock')
        with self.store.transaction() as db:
            self._prune(db)
            owner = db.execute('SELECT id,username,role FROM users WHERE id=? AND active=1', (actor['id'],)).fetchone()
            if owner is None:
                raise RuntimeError('Mission initiator is no longer active')
            if db.execute("SELECT count(*) FROM mission_runs WHERE outcome='ACTIVE'").fetchone()[0] >= 1000:
                raise RuntimeError('Mission capacity reached')
            db.execute('INSERT INTO mission_runs VALUES(?,?,?,?,?,?,?,?,?)',
                       (run_id,domain,owner['id'],owner['username'],owner['role'],route,now,None,'ACTIVE'))
            for hour in (-1, int(now//3600)*3600):
                db.execute('INSERT INTO mission_counts(domain,operator_id,hour,started) VALUES(?,?,?,1) '
                           'ON CONFLICT(domain,operator_id,hour) DO UPDATE SET started=started+1', (domain,owner['id'],hour))
            self.store._audit(db, dict(owner), 'mission.'+domain.lower()+'.started', 'OK', 'route:'+str(route))
        return run_id

    def finish(self, run_id, outcome, *, ended_at=None):
        if outcome not in OUTCOMES:
            raise ValueError('Unknown mission outcome')
        column = OUTCOMES[outcome]  # internal whitelist; no SQL identifier from HTTP
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM mission_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise RuntimeError('Unknown active mission')
            if row['outcome'] != 'ACTIVE':
                if row['outcome'] != outcome:
                    raise RuntimeError('Conflicting mission outcome')
                return False
            ended = self.store.clock() if ended_at is None else ended_at
            if not isinstance(ended, (int, float)) or not math.isfinite(ended):
                raise RuntimeError('Invalid mission END clock')
            db.execute('UPDATE mission_runs SET ended=?,outcome=? WHERE id=?', (ended,outcome,run_id))
            cursor = db.execute('UPDATE mission_counts SET '+column+'='+column+'+1 '
                                'WHERE domain=? AND operator_id=? AND hour=-1', (row['domain'],row['operator_id']))
            if cursor.rowcount != 1:
                raise RuntimeError('Missing all-time mission counter')
            # Result belongs to END hour, including when no mission started in that hour.
            db.execute('INSERT INTO mission_counts(domain,operator_id,hour,'+column+') VALUES(?,?,?,1) '
                       'ON CONFLICT(domain,operator_id,hour) DO UPDATE SET '+column+'='+column+'+1',
                       (row['domain'],row['operator_id'],int(ended//3600)*3600))
            actor = dict(id=row['operator_id'],username=row['operator_name'],role=row['operator_role'])
            self.store._audit(db, actor, 'mission.'+row['domain'].lower()+'.finished', outcome,
                              'route:'+str(row['route']))
            self._prune(db)
        return True

    def recover_demo(self):
        """Only at controlled process startup, never on a browser/page reload."""
        with self.store.transaction() as db:
            pending = [row[0] for row in db.execute("SELECT id FROM mission_runs WHERE domain='DEMO' AND outcome='ACTIVE'")]
        for run_id in pending:
            self.finish(run_id, 'INTERRUPTED')
        return len(pending)

    def report(self, period, *, operator_id=None, by_operator=False):
        now = self.store.clock()
        start, end = window(period, now)
        result = dict(schema_version=2, period=period, timezone='Europe/Kyiv', window_start=start, window_end=end,
                      as_of=now, operator_attribution='START_INITIATOR',
                      counter_attribution=dict(started='START_TIME', results='END_TIME', active='CURRENT_STATE_ALL_PERIODS'),
                      sources={})
        with self.store.transaction() as db:
            if operator_id is not None and not db.execute('SELECT 1 FROM users WHERE id=?', (operator_id,)).fetchone():
                raise LookupError('Unknown operator')
            migration = db.execute("SELECT value FROM mission_meta WHERE key='result_time_migration_at'").fetchone()
            migrated_at = float(migration[0]) if migration else None
            for domain in ('DEMO','REAL'):
                if domain == 'REAL' and not self.real_connected:
                    result['sources'][domain] = dict(available=False, reason='REAL_MISSION_SOURCE_NOT_CONNECTED',
                                                     coverage_from=None, summary=None, by_operator=[])
                    continue
                coverage = float(db.execute('SELECT value FROM mission_meta WHERE key=?', ('coverage_'+domain,)).fetchone()[0])
                owner_clause = ' AND operator_id=?' if operator_id is not None else ''
                owners = [operator_id] if operator_id is not None else []
                conditions, args = ['c.domain=?'], [domain]
                if period == 'all_time':
                    conditions.append('c.hour=-1')
                else:
                    conditions.extend(['c.hour>=?', 'c.hour<?'])
                    args.extend([start,end])
                if operator_id is not None:
                    conditions.append('c.operator_id=?')
                    args.append(operator_id)
                sums = ','.join('SUM(c.'+name+') AS '+name for name in COUNTERS)
                rows = db.execute('SELECT c.operator_id,u.username,'+sums+' FROM mission_counts c '
                                  'JOIN users u ON u.id=c.operator_id WHERE '+' AND '.join(conditions)+
                                  ' GROUP BY c.operator_id,u.username ORDER BY c.operator_id',args).fetchall()
                groups = {r['operator_id']:dict(operator_id=r['operator_id'],username=r['username'],
                          values={name:r[name] for name in COUNTERS},active=0,
                          gaps={name:0 for name in COUNTERS[1:]}) for r in rows}
                def group(owner):
                    if owner not in groups:
                        name = db.execute('SELECT username FROM users WHERE id=?',(owner,)).fetchone()[0]
                        groups[owner] = dict(operator_id=owner,username=name,values={k:0 for k in COUNTERS},
                                            active=0,gaps={k:0 for k in COUNTERS[1:]})
                    return groups[owner]
                # Gauge, not arithmetic on the selected period's independent counters.
                for r in db.execute("SELECT operator_id,count(*) AS n FROM mission_runs WHERE domain=? AND outcome='ACTIVE'"+
                                    owner_clause+' GROUP BY operator_id', [domain]+owners):
                    group(r['operator_id'])['active'] = r['n']
                for r in db.execute('SELECT * FROM mission_history_gaps WHERE domain=?'+owner_clause,[domain]+owners):
                    group(r['operator_id'])['gaps'] = {name:r[name] for name in COUNTERS[1:]}
                def complete(gaps):
                    return (period=='all_time' or not any(gaps.values()) or
                            (migrated_at is not None and start > migrated_at))
                totals = {name:sum(g['values'][name] for g in groups.values()) for name in COUNTERS}
                gaps = {name:sum(g['gaps'][name] for g in groups.values()) for name in COUNTERS[1:]}
                active_now = sum(g['active'] for g in groups.values())
                grouped = [dict(operator_id=g['operator_id'],username=g['username'],history_without_end_time=g['gaps'],
                                **metrics(g['values'],active=g['active'],results_complete=complete(g['gaps'])))
                           for _,g in sorted(groups.items())] if by_operator else []
                result['sources'][domain] = dict(available=True, reason=None, coverage_from=coverage,
                    result_time_migration_at=migrated_at, history_without_end_time=gaps,
                    summary=metrics(totals,active=active_now,results_complete=complete(gaps)),by_operator=grouped)
        return result
