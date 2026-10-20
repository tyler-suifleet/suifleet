#!/usr/bin/env python3
"""Clean-room SuiFleet function manager v1 (SF-069).

Routes durable native pub/sub messages through explicit source/subject
subscriptions to real function processes, on demand or long-lived, with
timeouts, bounded concurrency/queues, process isolation and versioned
invocation/result envelopes. Not AWS Lambda, Greengrass or their code.
"""
import argparse
import collections
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import selectors
import signal
import sqlite3
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk/python'))
from suifleet_ipc import Client, IpcError

VERSION = 1
MODES = ('on-demand', 'long-lived')
NAME = re.compile(r'[A-Za-z0-9_.-]{1,64}')
# Authenticated pub/sub senders: a component, @operator/UID or @bridge/NAME.
SOURCE = re.compile(r'[A-Za-z0-9_.-]{1,64}|@operator/[0-9]{1,10}|@bridge/[A-Za-z0-9_.-]{1,64}')
ENV_NAME = re.compile(r'[A-Z_][A-Z0-9_]{0,63}')
MAX_RESPONSE = 65536
MAX_DEPTH = 64               # response arrays/objects nesting
MAX_ENVELOPE = 4096          # native IPC publish payload limit
MAX_ERROR_TYPE, MAX_ERROR_MESSAGE = 128, 1024
LOG_BYTES = 1 << 20          # per function log, plus one rotated file
EXIT_GRACE = 1.0
CLEANUP_WAIT = 5.0           # SIGKILL to reaped, else the manager stops
DEDUPE_WINDOW = 10000
STATUSES = ('ok', 'error', 'timeout', 'busy', 'crashed', 'invalid-response',
            'unsupported-version', 'spawn-failed', 'unknown-function', 'result-too-large')

# Runs between fork/exec as the function's user: no core files, optional
# address-space bound, no inherited ambient capabilities or new privileges,
# killed with the spawning manager thread, then exec.
SHIM = ('import ctypes,os,resource,sys\n'
        'resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n'
        'm=int(sys.argv[2])\n'
        'm and resource.setrlimit(resource.RLIMIT_AS,(m,m))\n'
        'c=ctypes.CDLL(None,use_errno=True)\n'
        'c.prctl(47,4,0,0,0)==0 or os._exit(71)\n'
        'c.prctl(38,1,0,0,0)==0 or os._exit(72)\n'
        'c.prctl(1,9)\n'
        'os.getppid()==int(sys.argv[1]) or os._exit(70)\n'
        'os.chdir(sys.argv[3])\n'
        'os.execv(sys.argv[4],sys.argv[4:])\n')

# Runs as a function's user when the manager may not signal it directly (a
# non-root manager with only CAP_SETUID/SETGID): SIGKILL each gPGID / pPID.
KILLER = ('import os,signal,sys\n'
          'for a in sys.argv[1:]:\n'
          ' try:(os.killpg if a[0]=="g" else os.kill)(int(a[1:]),signal.SIGKILL)\n'
          ' except ProcessLookupError:pass\n')


# --- Pure decisions shared by the runtime and the SF-064 v1 subcontract ------

def supported(value):
    return type(value.get('version')) is int and value['version'] == VERSION


def timed_out(elapsed_ms, timeout_ms):
    return elapsed_ms >= timeout_ms


def admit(running, waiting, max_concurrency, max_queue):
    """FIFO admission: invoke below the bound, else queue, else busy."""
    if running < max_concurrency and waiting == 0:
        return 'invoke'
    return 'queue' if waiting < max_queue else 'busy'


def route(subscriptions, source, subject):
    """Exact source/subject matches; sorted, unique function targets."""
    return sorted({s['target'] for s in subscriptions if s['source'] == source and s['subject'] == subject})


def reference_decision(request):
    """The SF-064 lambda v1 vector computed from this runtime's decisions (queue 0)."""
    if not supported(request):
        return {'status': 'unsupported-version'}
    if request.get('authorized') is not True:
        return {'status': 'denied'}
    if request.get('mode') not in MODES:
        return {'status': 'invalid-mode'}
    if timed_out(request.get('elapsed_ms', 0), request['timeout_ms']):
        return {'status': 'timeout'}
    if admit(request['active'], 0, request['concurrency'], 0) == 'busy':
        return {'status': 'busy'}
    return {'status': 'invoke', 'targets': route(request['subscriptions'], request['source'], request['subject']),
            'payload': request['payload']}


def _no_constant(name):
    raise ValueError(f'{name} is not JSON')


def nested_within(value, limit):
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > limit:
                return False
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return True


def parse_response(line, request):
    """Validate one function response line; returns (status, fields).

    Only strict UTF-8 JSON is accepted: no NaN/Infinity, lone surrogates or
    nesting beyond MAX_DEPTH, so every accepted value re-serializes."""
    try:
        value = json.loads(line.decode('utf-8') if isinstance(line, bytes) else line, parse_constant=_no_constant)
        if not nested_within(value, MAX_DEPTH):
            return 'invalid-response', {}
        compact(value).encode('utf-8')
    except (ValueError, RecursionError):   # includes Unicode errors
        return 'invalid-response', {}
    if not isinstance(value, dict):
        return 'invalid-response', {}
    if not supported(value):
        return 'unsupported-version', {}
    if value.get('invocation_id') != request['invocation_id']:
        return 'invalid-response', {}
    if value.get('status') == 'ok' and set(value) == {'version', 'invocation_id', 'status', 'result'}:
        return 'ok', {'result': value['result']}
    error = value.get('error')
    if (value.get('status') == 'error' and set(value) == {'version', 'invocation_id', 'status', 'error'}
            and isinstance(error, dict) and set(error) == {'type', 'message'}
            and isinstance(error['type'], str) and 0 < len(error['type']) <= MAX_ERROR_TYPE
            and isinstance(error['message'], str) and len(error['message']) <= MAX_ERROR_MESSAGE):
        return 'error', {'error': error}
    return 'invalid-response', {}


def compact(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True, ensure_ascii=False, allow_nan=False)


# --- Configuration -----------------------------------------------------------

def topic(value):
    return (isinstance(value, str) and 0 < len(value.encode()) <= 256
            and not set(value) & {'+', '#', '\0'})


def positive(value, high):
    return type(value) is int and 1 <= value <= high


def load_config(value):
    required = {'version', 'socket', 'component', 'state_dir', 'work_dir', 'functions', 'subscriptions'}
    if not isinstance(value, dict) or not required <= set(value) <= required | {'max_pending'}:
        raise ValueError('invalid configuration fields')
    if not supported(value):
        raise ValueError('unsupported configuration version')
    for key in ('socket', 'state_dir', 'work_dir'):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ValueError('paths must be absolute')
    if not isinstance(value['component'], str) or not NAME.fullmatch(value['component']):
        raise ValueError('invalid component')
    if not positive(value.get('max_pending', 256), 10000):
        raise ValueError('invalid max_pending')
    functions = value['functions']
    if not isinstance(functions, dict) or not 1 <= len(functions) <= 64:
        raise ValueError('between 1 and 64 functions')
    for name, fn in functions.items():
        allowed = {'mode', 'command', 'timeout_ms', 'max_concurrency', 'max_queue', 'result_topic',
                   'environment', 'run_as', 'memory_bytes'}
        if not NAME.fullmatch(name) or not isinstance(fn, dict) or not {'mode', 'command', 'timeout_ms'} <= set(fn) <= allowed:
            raise ValueError(f'invalid function {name!r}')
        if fn['mode'] not in MODES:
            raise ValueError('invalid-mode')
        command = fn['command']
        if (not isinstance(command, list) or not command or not all(isinstance(a, str) and a and '\0' not in a for a in command)
                or not Path(command[0]).is_absolute()):
            raise ValueError('command must be an argv list with an absolute executable')
        if not positive(fn['timeout_ms'], 900000):
            raise ValueError('timeout_ms must be 1..900000')
        concurrency = fn.setdefault('max_concurrency', 1)
        if not positive(concurrency, 64) or (fn['mode'] == 'long-lived' and concurrency != 1):
            raise ValueError('max_concurrency must be 1..64, and 1 for long-lived')
        queued = fn.setdefault('max_queue', 1000)
        if type(queued) is not int or not 0 <= queued <= 10000:
            raise ValueError('max_queue must be 0..10000')
        if 'result_topic' in fn and not topic(fn['result_topic']):
            raise ValueError('invalid result_topic')
        environment = fn.setdefault('environment', {})
        if (not isinstance(environment, dict) or len(environment) > 64
                or not all(ENV_NAME.fullmatch(k) and not k.startswith('SUIFLEET_') and isinstance(v, str) and '\0' not in v
                           for k, v in environment.items())):
            raise ValueError('invalid environment')
        run_as = fn.get('run_as')
        if run_as is not None and (not isinstance(run_as, dict) or set(run_as) != {'uid', 'gid'}
                                   or not all(positive(run_as[k], 2**31 - 2) for k in ('uid', 'gid'))):
            raise ValueError('run_as needs nonzero uid and gid')
        memory = fn.get('memory_bytes', 0)
        if type(memory) is not int or (memory and not 16 << 20 <= memory <= 1 << 40):
            raise ValueError('memory_bytes must be 16 MiB..1 TiB')
    subscriptions = value['subscriptions']
    if not isinstance(subscriptions, list) or not 1 <= len(subscriptions) <= 256:
        raise ValueError('between 1 and 256 subscriptions')
    for subscription in subscriptions:
        if not isinstance(subscription, dict) or set(subscription) != {'source', 'subject', 'target'}:
            raise ValueError('subscription needs source, subject and target')
        if not isinstance(subscription['source'], str) or not SOURCE.fullmatch(subscription['source']):
            raise ValueError('invalid subscription source')
        if subscription['source'] == value['component']:
            raise ValueError('the manager cannot be a subscription source')
        if not topic(subscription['subject']):
            raise ValueError('subject must be an exact topic name')
        if subscription['target'] not in functions:
            raise ValueError('subscription target is not a configured function')
    return value


# --- Durable journal -----------------------------------------------------------

class Journal:
    """Accepted messages and per-function invocation state, fsynced (SQLite FULL)."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
                subject TEXT NOT NULL, sender TEXT NOT NULL, message_id INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS invocations(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
                function TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','running','completed','published')),
                attempt INTEGER NOT NULL DEFAULT 0, envelope TEXT, UNIQUE(key, function));
            CREATE INDEX IF NOT EXISTS invocations_state ON invocations(state, id);''')

    def bind(self, binding):
        row = self.db.execute("SELECT value FROM meta WHERE name='binding'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES('binding', ?)", (binding,))
        elif row[0] != binding:
            raise ValueError('state belongs to another socket/component')

    def accept(self, key, message, targets):
        """Record a message and its invocations once; False for a redelivery."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute('SELECT 1 FROM inbox WHERE key=?', (key,)).fetchone():
                self.db.execute('COMMIT')
                return False
            self.db.execute('INSERT INTO inbox(key, subject, sender, message_id, payload) VALUES(?,?,?,?,?)',
                            (key, message['topic'], message['sender'], message['id'], compact(message.get('payload'))))
            self.db.executemany("INSERT INTO invocations(key, function, state) VALUES(?,?,'pending')",
                                [(key, target) for target in targets])
            self.db.execute('COMMIT')
            return True
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def rows(self, state):
        return self.db.execute('''SELECT i.id, i.key, i.function, i.attempt, i.envelope, m.subject, m.sender,
                                  m.message_id, m.payload FROM invocations i JOIN inbox m ON m.key = i.key
                                  WHERE i.state = ? ORDER BY i.id''', (state,)).fetchall()

    def start(self, row_id):
        self.db.execute("UPDATE invocations SET state='running', attempt=attempt+1 WHERE id=?", (row_id,))
        return self.db.execute('SELECT attempt FROM invocations WHERE id=?', (row_id,)).fetchone()[0]

    def complete(self, row_id, envelope):
        self.db.execute("UPDATE invocations SET state='completed', envelope=? WHERE id=?", (compact(envelope), row_id))

    def published(self, row_id):
        self.db.execute("UPDATE invocations SET state='published' WHERE id=?", (row_id,))

    def recover(self):
        """Interrupted invocations run again; their attempt number reveals the retry."""
        return self.db.execute("UPDATE invocations SET state='pending' WHERE state='running'").rowcount

    def outstanding(self):
        return self.db.execute("SELECT COUNT(*) FROM invocations WHERE state != 'published'").fetchone()[0]

    def prune(self):
        self.db.execute('''DELETE FROM inbox WHERE seq <= (SELECT MAX(seq) FROM inbox) - ?
                           AND key NOT IN (SELECT key FROM invocations WHERE state != 'published')''', (DEDUPE_WINDOW,))
        self.db.execute('DELETE FROM invocations WHERE key NOT IN (SELECT key FROM inbox)')

    def close(self):
        self.db.close()


# --- Processes -----------------------------------------------------------------

def read_line(fd, buffer, deadline):
    """Read one LF-terminated line before a monotonic deadline.

    Returns (line, reason) where reason is None, 'eof', 'timeout' or 'too-large'.
    """
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while b'\n' not in buffer:
            if len(buffer) > MAX_RESPONSE:
                return None, 'too-large'
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None, 'timeout'
            chunk = os.read(fd, 65536)
            if not chunk:
                return None, 'eof'
            buffer.extend(chunk)
    line, _, rest = bytes(buffer).partition(b'\n')
    buffer[:] = rest
    if len(line) > MAX_RESPONSE:
        return None, 'too-large'
    return line, None


class LogSink(threading.Thread):
    """Bounded per-function stderr log with one rotated generation."""

    locks = collections.defaultdict(threading.Lock)

    def __init__(self, stream, path):
        super().__init__(daemon=True)
        self.stream, self.path = stream, path

    def run(self):
        with self.stream:
            for chunk in iter(lambda: self.stream.read1(65536), b''):
                with self.locks[self.path]:
                    try:
                        if self.path.exists() and self.path.stat().st_size + len(chunk) > LOG_BYTES:
                            os.replace(self.path, self.path.with_suffix('.log.1'))
                        with open(self.path, 'ab') as log:
                            log.write(chunk[:LOG_BYTES])
                    except OSError:
                        pass


class CleanupError(RuntimeError):
    """A function process could not be killed and reaped; the manager must stop."""


class Spawner:
    """Starts isolated function processes; the manager is their subreaper."""

    def __init__(self, logs, work):
        self.logs, self.work = logs, work
        # Reentrant: sweep() kills through terminate(); a helper must not be swept.
        self.lock, self.tracked, self.owners = threading.RLock(), set(), {}
        self.failed = None
        if sys.platform.startswith('linux'):
            ctypes.CDLL(None, use_errno=True).prctl(36, 1)  # PR_SET_CHILD_SUBREAPER

    def spawn(self, name, fn):
        run_as = fn.get('run_as')
        env = dict(fn['environment'], PATH='/usr/local/bin:/usr/bin:/bin', LANG='C.UTF-8',
                   HOME=str(self.work / name), SUIFLEET_FUNCTION=name, SUIFLEET_FUNCTION_MODE=fn['mode'])
        # The shim enters the private work directory after the identity change.
        argv = [sys.executable, '-I', '-c', SHIM, str(os.getpid()), str(fn.get('memory_bytes', 0)),
                str(self.work / name), *fn['command']]
        with self.lock:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd='/', env=env, close_fds=True, start_new_session=True,
                                       user=run_as and run_as['uid'], group=run_as and run_as['gid'],
                                       extra_groups=[] if run_as else None)
            self.tracked.add(process.pid)
            self.owners[process.pid] = (run_as['uid'], run_as['gid']) if run_as else (os.getuid(), os.getgid())
        LogSink(process.stderr, self.logs / f'{name}.log').start()
        return process

    def fail(self, text):
        self.failed = self.failed or text
        return CleanupError(text)

    def terminate(self, targets, owner):
        """SIGKILL ('g', PGID) groups and ('p', PID) processes. Signals the manager
        may not send directly are sent by a helper running as their owner."""
        denied = []
        for kind, ident in targets:
            try:
                (os.killpg if kind == 'g' else os.kill)(ident, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                denied.append(f'{kind}{ident}')
        if not denied:
            return
        with self.lock:
            try:
                subprocess.run([sys.executable, '-I', '-c', KILLER, *denied], user=owner[0], group=owner[1],
                               extra_groups=[], env={}, cwd='/', close_fds=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=CLEANUP_WAIT, check=True)
            except (OSError, subprocess.SubprocessError) as error:
                raise self.fail(f'cannot kill {" ".join(denied)} as {owner[0]}: {type(error).__name__}') from None

    def release(self, process):
        """Kill the process group (descendants included) and reap, or fail."""
        self.terminate([('g', process.pid)], self.owners.get(process.pid, (os.getuid(), os.getgid())))
        try:
            process.wait(CLEANUP_WAIT)
        except subprocess.TimeoutExpired:
            raise self.fail(f'process {process.pid} survived SIGKILL') from None
        for stream in (process.stdin, process.stdout):
            if stream and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        with self.lock:
            self.tracked.discard(process.pid)
            self.owners.pop(process.pid, None)
        self.sweep()

    def sweep(self):
        """Kill descendants that escaped their group and were reparented to us."""
        me = os.getpid()
        with self.lock:
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit() or int(entry.name) in self.tracked:
                    continue
                try:
                    stat = (entry / 'stat').read_text()
                    status = dict(line.split(':', 1) for line in (entry / 'status').read_text().splitlines() if ':' in line)
                except OSError:
                    continue
                if int(stat.rpartition(')')[2].split()[1]) != me:
                    continue
                pid = int(entry.name)
                self.terminate([('p', pid)], (int(status['Uid'].split()[0]), int(status['Gid'].split()[0])))
                deadline = time.monotonic() + CLEANUP_WAIT
                try:
                    while os.waitpid(pid, os.WNOHANG) == (0, 0):
                        if time.monotonic() > deadline:
                            raise self.fail(f'escaped process {pid} survived SIGKILL')
                        time.sleep(0.01)
                except ChildProcessError:
                    pass


def exit_detail(process):
    code = process.poll()
    return {'exit_code': code} if code is None or code >= 0 else {'signal': -code}


def exchange(process, buffer, request, deadline, last=False):
    """Send one invocation and await its response; returns (status, fields)."""
    try:
        process.stdin.write(compact(request).encode() + b'\n')
        process.stdin.flush()
        if last:
            process.stdin.close()
    except (BrokenPipeError, OSError):
        return 'crashed', {}
    line, reason = read_line(process.stdout.fileno(), buffer, deadline)
    if reason == 'timeout':
        return 'timeout', {}
    if reason == 'too-large':
        return 'invalid-response', {}
    if reason == 'eof':
        return 'crashed', {}
    return parse_response(line, request)


class Worker(threading.Thread):
    """One long-lived function process: started with the manager, serial invocations,
    restarted with backoff after exit, timeout or a protocol violation."""

    def __init__(self, manager, name, fn):
        super().__init__(daemon=True)
        self.manager, self.name, self.fn = manager, name, fn
        self.inbox, self.process, self.starts = queue.Queue(), None, 0

    def run(self):
        try:
            self.serve()
        except CleanupError:
            pass        # the main loop stops the manager

    def serve(self):
        backoff, next_start, held = 1, 0.0, None
        while not self.manager.stopping.is_set():
            if self.process is not None and self.process.poll() is not None:
                self.manager.spawner.release(self.process)
                self.process = None
                self.manager.diagnostic(f'{self.name}: long-lived process exited; restarting')
                next_start, backoff = time.monotonic() + backoff, min(30, backoff * 2)
            if self.process is None and time.monotonic() >= next_start:
                try:
                    self.process, self.buffer, self.started = self.manager.spawner.spawn(self.name, self.fn), bytearray(), time.monotonic()
                    self.starts += 1
                except OSError as error:
                    self.manager.diagnostic(f'{self.name}: spawn-failed ({error.strerror})')
                    next_start, backoff = time.monotonic() + backoff, min(30, backoff * 2)
            if held is None:
                try:
                    held = self.inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
            row, request, dispatched = held
            if time.monotonic() - dispatched >= self.fn['timeout_ms'] / 1000:
                self.manager.finish(row, request, 'timeout', {}, dispatched)   # never became available
                held = None
                continue
            if self.process is None:
                time.sleep(0.05)
                continue
            held = None
            status, fields = exchange(self.process, self.buffer, request, dispatched + self.fn['timeout_ms'] / 1000)
            if status not in ('ok', 'error'):
                fields.update(exit_detail(self.process))
                self.manager.spawner.release(self.process)
                self.process = None
                next_start = time.monotonic() + (0 if status == 'timeout' else backoff)
                backoff = 1 if status == 'timeout' else min(30, backoff * 2)
            elif time.monotonic() - self.started > 10:
                backoff = 1
            self.manager.finish(row, request, status, fields, dispatched)
        if self.process is not None:
            self.manager.spawner.release(self.process)


def on_demand(manager, name, fn, row, request, dispatched):
    """One new process per invocation; the whole group is killed afterwards."""
    try:
        process = manager.spawner.spawn(name, fn)
    except OSError as error:
        manager.diagnostic(f'{name}: spawn-failed ({error.strerror})')
        return manager.finish(row, request, 'spawn-failed', {}, dispatched)
    try:
        status, fields = exchange(process, bytearray(), request, dispatched + fn['timeout_ms'] / 1000, last=True)
        if status in ('ok', 'error', 'crashed'):   # let it exit; the group is killed regardless
            try:
                process.wait(EXIT_GRACE)
            except subprocess.TimeoutExpired:
                pass
            if status == 'crashed':
                fields.update(exit_detail(process))
    finally:
        try:
            manager.spawner.release(process)
        except CleanupError:
            return      # the main loop stops the manager
    manager.finish(row, request, status, fields, dispatched)


# --- Manager -------------------------------------------------------------------

class Manager:
    def __init__(self, cfg, ipc, log=None):
        self.cfg, self.ipc = load_config(cfg), ipc
        self.functions = cfg['functions']
        self.subscriptions = cfg['subscriptions']
        self.subjects = sorted({s['subject'] for s in self.subscriptions})
        self.stopping, self.results = threading.Event(), queue.Queue()
        self.running = collections.Counter()
        self.log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        if os.geteuid() == 0 and not all(fn.get('run_as') for fn in self.functions.values()):
            raise ValueError('a root manager requires run_as for every function')
        self.state = Path(cfg['state_dir'])
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.state.lstat()
        if self.state.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('state directory must be private and owned')
        self.lock = open(self.state / 'lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.journal = Journal(self.state / 'journal.sqlite3')
            self.journal.bind(hashlib.sha256(compact([cfg['socket'], cfg['component']]).encode()).hexdigest())
        except Exception:
            self.lock.close()
            raise
        (self.state / 'logs').mkdir(mode=0o700, exist_ok=True)
        work = Path(cfg['work_dir'])
        work.mkdir(mode=0o711, parents=True, exist_ok=True)
        if work.is_symlink() or work.lstat().st_uid != os.getuid() or work.lstat().st_mode & 0o022:
            raise ValueError('work directory must be owned and not group/world writable')
        for name, fn in self.functions.items():
            directory = work / name
            uid, gid = (fn['run_as']['uid'], fn['run_as']['gid']) if fn.get('run_as') else (os.getuid(), os.getgid())
            if not directory.exists():
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
                if uid != os.getuid():
                    os.chown(directory, uid, gid)   # needs root or CAP_CHOWN; or pre-provision
            st = directory.lstat()
            if directory.is_symlink() or not directory.is_dir() or st.st_uid != uid or st.st_mode & 0o077:
                raise ValueError('function work directory must be private to its user')
        self.spawner = Spawner(self.state / 'logs', work)
        self.recovered = self.journal.recover()
        self.workers = {}
        self.threads = set()
        self.publish_after, self.publish_backoff = 0.0, 0.5
        self.retired, self.prune_after = True, 0.0

    def diagnostic(self, text):
        self.log(f'functions: {text}')

    def start(self):
        for subject in self.subjects:
            self.ipc.subscribe(subject, qos=1)
        for name, fn in self.functions.items():
            if fn['mode'] == 'long-lived':
                self.workers[name] = Worker(self, name, fn)
                self.workers[name].start()

    def request(self, row, fn, attempt):
        return dict(version=VERSION, invocation_id=f"{row['key']}/{row['function']}", function=row['function'],
                    mode=fn['mode'], source=row['sender'], subject=row['subject'], message_id=row['message_id'],
                    attempt=attempt, timeout_ms=fn['timeout_ms'], payload=json.loads(row['payload']))

    def envelope(self, row, attempt, status, fields, duration_ms=0):
        value = dict(version=VERSION, invocation_id=f"{row['key']}/{row['function']}", function=row['function'],
                     source=row['sender'], subject=row['subject'], message_id=row['message_id'], attempt=attempt,
                     status=status, duration_ms=duration_ms, **fields)
        try:
            size = len(compact(value).encode())
        except (ValueError, RecursionError):   # defense in depth; parse_response rejects these
            size = None
        if size is None or size > MAX_ENVELOPE:
            value = {k: v for k, v in value.items() if k not in ('result', 'error')}
            value['status'] = 'invalid-response' if size is None else 'result-too-large'
        return value

    def finish(self, row, request, status, fields, dispatched):
        """Called by invocation threads; the main loop persists the outcome."""
        elapsed = int((time.monotonic() - dispatched) * 1000)
        if status in ('ok', 'error') and timed_out(elapsed, request['timeout_ms']):
            status, fields = 'timeout', {}
        self.results.put((row, request['attempt'], status, fields, elapsed))

    def collect(self):
        while True:
            try:
                row, attempt, status, fields, elapsed = self.results.get_nowait()
            except queue.Empty:
                return
            self.running[row['function']] -= 1
            self.journal.complete(row['id'], self.envelope(row, attempt, status, fields, elapsed))

    def dispatch(self):
        waiting = collections.Counter()
        for row in self.journal.rows('pending'):
            name = row['function']
            fn = self.functions.get(name)
            if fn is None:
                self.journal.complete(row['id'], self.envelope(row, row['attempt'], 'unknown-function', {}))
                continue
            decision = admit(self.running[name], waiting[name], fn['max_concurrency'], fn['max_queue'])
            if decision == 'queue':
                waiting[name] += 1
                continue
            if decision == 'busy':
                self.journal.complete(row['id'], self.envelope(row, row['attempt'], 'busy', {}))
                continue
            attempt = self.journal.start(row['id'])
            self.running[name] += 1
            request, dispatched = self.request(row, fn, attempt), time.monotonic()
            if fn['mode'] == 'long-lived':
                self.workers[name].inbox.put((row, request, dispatched))
            else:
                thread = threading.Thread(target=on_demand, args=(self, name, fn, row, request, dispatched), daemon=True)
                self.threads.add(thread)
                thread.start()
        self.threads = {t for t in self.threads if t.is_alive()}

    def intake(self, subject):
        message = self.ipc.receive(subject)
        if message is None:
            return False
        if type(message.get('id')) is not int:
            self.diagnostic('ignored a legacy inbox message without an id; use durable subscriptions only')
            return True
        if message.get('topic') != subject or not isinstance(message.get('sender'), str):
            self.diagnostic('malformed message acknowledged without invocation')
            self.ipc.ack(subject, message['id'])
            return True
        try:
            compact(message.get('payload')).encode()
        except (ValueError, RecursionError):
            self.diagnostic('non-JSON payload acknowledged without invocation')
            self.ipc.ack(subject, message['id'])
            return True
        targets = route(self.subscriptions, message.get('sender'), message.get('topic'))
        if not self.journal.accept(f"{subject}#{message['id']}", message, targets):
            self.diagnostic('duplicate delivery acknowledged without invocation')
        self.ipc.ack(subject, message['id'])
        return True

    def publish(self):
        if time.monotonic() < self.publish_after:
            return
        for row in self.journal.rows('completed'):
            fn = self.functions.get(row['function'], {})
            if 'result_topic' in fn:
                try:
                    self.ipc.publish(fn['result_topic'], json.loads(row['envelope']))
                except (IpcError, OSError) as error:
                    code = str(error) if isinstance(error, IpcError) else 'unavailable'
                    self.diagnostic(f'result retained ({code[:64]})')
                    self.publish_after = time.monotonic() + self.publish_backoff
                    self.publish_backoff = min(30, self.publish_backoff * 2)
                    return
            self.journal.published(row['id'])
            self.publish_backoff = 0.5
            self.retired = True

    def step(self):
        if self.spawner.failed:
            raise CleanupError(self.spawner.failed)
        self.collect()
        self.dispatch()
        progressed = False
        for subject in self.subjects:
            if self.journal.outstanding() < self.cfg.get('max_pending', 256) and self.intake(subject):
                progressed = True
                self.collect()
                self.dispatch()
        self.publish()
        if self.retired and time.monotonic() >= self.prune_after:
            self.journal.prune()
            self.retired, self.prune_after = False, time.monotonic() + 10
        return progressed

    def close(self, grace=10):
        deadline = time.monotonic() + (0 if self.spawner.failed else grace)
        while (self.threads or any(self.running.values())) and time.monotonic() < deadline:
            self.collect()
            self.threads = {t for t in self.threads if t.is_alive()}
            time.sleep(0.05)
        self.stopping.set()
        for worker in self.workers.values():
            worker.join(5)
        self.collect()
        self.journal.close()
        self.lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        cfg = load_config(json.loads(Path(args.config).read_text()))
        manager = Manager(cfg, Client(cfg['socket'], cfg['component'], timeout=30))
        manager.start()
    except Exception as error:  # noqa: BLE001 - payload-free startup diagnostic
        sys.exit(f'function manager startup rejected: {type(error).__name__}: {str(error)[:200]}')
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    delay, idle, exit_status = 0.2, 0.02, 0
    try:
        while not stop.is_set():
            try:
                progressed = manager.step()
                delay = 0.2
                # Receive is nonblocking: poll quickly while busy, back off to 250 ms idle.
                idle = 0.02 if progressed else min(0.25, idle * 2)
                if not progressed:
                    stop.wait(idle)
            except (IpcError, OSError) as error:
                code = str(error) if isinstance(error, IpcError) else 'unavailable'
                manager.diagnostic(f'ipc retry ({code[:64]})')
                stop.wait(delay)
                delay = min(30, delay * 2)
            except CleanupError as error:
                # Never continue with an unkilled function: stop, and let the platform
                # unit's control-group kill end it; interrupted work reruns on restart.
                manager.diagnostic(f'stopping: {error}')
                exit_status = 1
                break
    finally:
        manager.close()
    sys.exit(exit_status)


if __name__ == '__main__':
    main()
