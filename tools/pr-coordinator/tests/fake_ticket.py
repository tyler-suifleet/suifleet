#!/usr/bin/env python3
"""Offline stand-in for the atomic `ticket` lock commands used by SF-116 tests.

It mirrors the installed engine's lease semantics: a live lock held by another
session is refused, an expired one is taken over, the same session re-locking
renews and keeps its token, and renew/assert/unlock require the exact token.
One SQLite file is shared by the in-process dispatcher fake and real worker
processes, so exclusivity is enforced across processes as in production.
"""
import json
import sqlite3
import sys
import time
import uuid


class Refused(Exception):
    pass


class Locks:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS locks (resource TEXT PRIMARY KEY, token TEXT, agent TEXT,
                           model TEXT, session TEXT, ticket TEXT, purpose TEXT, expires REAL)''')

    def transaction(self, action):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = action()
            self.db.execute('COMMIT')
            return result
        finally:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')

    def view(self, row, token=False):
        item = {k: row[k] for k in row.keys() if token or k != 'token'}
        item['expired'] = row['expires'] <= self.clock()
        return item

    def lock(self, resource, agent, model, session, purpose, ticket=None, ttl=900):
        def action():
            now = self.clock()
            old = self.db.execute('SELECT * FROM locks WHERE resource=?', (resource,)).fetchone()
            if old and old['expires'] > now and old['session'] != session:
                raise Refused(f'{resource} is locked by session {old["session"]}')
            if old and old['session'] == session and old['expires'] > now:
                self.db.execute('UPDATE locks SET expires=?,purpose=? WHERE resource=?', (now + ttl, purpose, resource))
            else:
                self.db.execute('INSERT OR REPLACE INTO locks VALUES(?,?,?,?,?,?,?,?)',
                                (resource, uuid.uuid4().hex, agent, model, session, ticket, purpose, now + ttl))
            return self.view(self.db.execute('SELECT * FROM locks WHERE resource=?', (resource,)).fetchone(), token=True)
        return self.transaction(action)

    def held(self, resource, token):
        row = self.db.execute('SELECT * FROM locks WHERE resource=?', (resource,)).fetchone()
        if not row or row['token'] != token or row['expires'] <= self.clock():
            raise Refused(f'token no longer holds {resource}')
        return row

    def renew(self, resource, token, ttl=900):
        def action():
            self.held(resource, token)
            self.db.execute('UPDATE locks SET expires=? WHERE resource=?', (self.clock() + ttl, resource))
            return self.view(self.db.execute('SELECT * FROM locks WHERE resource=?', (resource,)).fetchone())
        return self.transaction(action)

    def assert_(self, resource, token):
        return self.transaction(lambda: self.view(self.held(resource, token)))

    def unlock(self, resource, token):
        def action():
            row = self.db.execute('SELECT * FROM locks WHERE resource=?', (resource,)).fetchone()
            if not row or row['token'] != token:
                raise Refused(f'token does not hold {resource}')
            self.db.execute('DELETE FROM locks WHERE resource=?', (resource,))
            return {'unlocked': resource}
        return self.transaction(action)

    def list(self):
        return [self.view(r) for r in self.db.execute('SELECT * FROM locks ORDER BY resource')]

    def command(self, args):
        """Execute `ticket` argv after `--project NAME`."""
        options = {args[n][2:]: args[n+1] for n in range(2, len(args) - 1) if args[n].startswith('--')}
        if args[0] == 'locks':
            return self.list()
        if args[0] == 'lock':
            return self.lock(args[1], options['agent'], options['model'], options['session'], options['purpose'],
                             options.get('ticket'), int(options.get('ttl', 900)))
        if args[0] == 'lock-renew':
            return self.renew(args[1], options['lock-token'], int(options.get('ttl', 900)))
        if args[0] == 'lock-assert':
            return self.assert_(args[1], options['lock-token'])
        if args[0] == 'unlock':
            return self.unlock(args[1], options['lock-token'])
        raise ValueError(f'unsupported fake ticket command {args[0]}')


if __name__ == '__main__':
    argv = sys.argv[1:]
    if argv[:1] == ['--db']:
        path, argv = argv[1], argv[2:]
    if argv[:1] == ['--project']:
        argv = argv[2:]
    try:
        print(json.dumps(Locks(path).command(argv)))
    except Refused as error:
        sys.exit(f'Coordination stopped: {error}')
