"""Real Git source authentication and service-boundary supervisor upgrades."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('upgrade_activation', SOURCE/'upgrade_activation.py')
u = importlib.util.module_from_spec(spec); spec.loader.exec_module(u)
spec = importlib.util.spec_from_file_location('upgrade_activation_fixture_supervisor', SOURCE/'activation.py')
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)


class Services:
    def __init__(self, fixture):
        self.fixture = fixture
        self.active_units = list(a.SERVICES)
        self.stops, self.starts = [], []
        self.fail_start = False

    def active(self):
        self.fixture.events.append('active')
        return self.active_units[:]

    def stop(self, units):
        self.stops.append(list(units))

    def start(self, units, generation):
        # A real new parent must be able to acquire these locks before ready.
        with u.authority_locks(self.fixture.state): pass
        self.starts.append((list(units), str(generation)))
        if self.fail_start:
            self.fail_start = False
            raise RuntimeError('startup readiness failed')


class UpgradeActivation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home/'repo'; self.repo.mkdir()
        self.source = self.repo/a.PREFIX; self.source.mkdir(parents=True)
        self.git('init', '-q'); self.git('config', 'user.name', 'Tyler Bush'); self.git('config', 'user.email', 'tyler.jonathan.bush@gmail.com')
        self.main = self.git('branch', '--show-current')
        for name in ('activation.py', 'upgrade_activation.py', 'install.py', 'coordinator.py', 'dispatcher.py', 'worker.py', 'batch_integration.py', 'dashboard.py'):
            (self.source/name).write_text('# reviewed '+name+'\n')
        (self.source/'runtime-compatibility.json').write_text(json.dumps(a.COMPATIBILITY))
        self.commit('base'); self.base = self.git('rev-parse', 'HEAD')
        (self.source/'activation.py').write_text('# reviewed supervisor upgrade\n')
        self.commit('reviewed normal integration'); self.target = self.git('rev-parse', 'HEAD')
        self.library = self.home/'.local/lib/pr-coordinator'; self.library.mkdir(parents=True)
        self.state = self.home/'.local/state/pr-coordinator'; self.state.mkdir(parents=True)
        self.previous = self.library/'generations/old'; self.previous.mkdir(parents=True)
        (self.previous/'coordinator.py').write_text('# installed coordinator\n')
        (self.previous/'installed.json').write_text(json.dumps(dict(revision=self.base, hashes={'coordinator.py':hashlib.sha256((self.previous/'coordinator.py').read_bytes()).hexdigest()})))
        (self.library/'current').symlink_to(self.previous)
        self.supervisor = self.library/'activation.py'; self.old_bytes = b'# installed trusted supervisor\n'; self.supervisor.write_bytes(self.old_bytes)
        self.dashboard = self.library/'dashboard.py'; self.old_dashboard = b'# installed dashboard\n'; self.dashboard.write_bytes(self.old_dashboard)
        self.config_dir = self.home/'.config/pr-coordinator'; self.config_dir.mkdir(parents=True)
        self.settings = dict(project='p', root=str(self.repo), repository='owner/repo', github_account='account')
        self.private = self.config_dir/'config.json'; self.private.write_text(json.dumps({'projects':[dict(id='p',root=str(self.repo),repository='owner/repo',github_account='account')],'workers':{'private':'preserve'}}))
        (self.config_dir/'activation.json').write_text(json.dumps(self.settings))
        self.private_bytes = self.private.read_bytes()
        self.services = Services(self)
        self.events = []
        self.reject_old = False; self.reject_new = False; self.reject_validation = False
        self.mutate_source = False; self.move_tip = False; self.tip_calls = 0
        fixture = self

        class Engine:
            def __init__(self, settings, library, state, services=None, role='old'):
                self.settings,self.library,self.state,self.root = settings,library,state,Path(settings['root'])
                self.link = library/'current'; self.journal=state/'activation.json'
                self.services=services or fixture.services; self.role=role
            def git(self,*args):return fixture.git(*args)
            def tip(self):
                fixture.tip_calls += 1
                if fixture.move_tip and fixture.tip_calls>1:return 'f'*40
                return fixture.target
            def verify(self,old,target):
                fixture.events.append(self.role+'-verify')
                if ((self.role=='old' and fixture.reject_old) or (self.role=='new' and fixture.reject_new)):
                    raise RuntimeError(self.role+' verifier rejected unreviewed merge')
                if self.role=='old' and fixture.mutate_source:
                    (fixture.source/'activation.py').write_text('# dirty changed supervisor\n')
            def validate(self,target):
                fixture.events.append('validate')
                if fixture.reject_validation:raise RuntimeError('offline validation failed')
            def stage(self,target):
                fixture.events.append('stage')
                generation=self.library/'generations/new'; generation.mkdir()
                (generation/'coordinator.py').write_text('# reviewed coordinator\n')
                (generation/'installed.json').write_text(json.dumps(dict(revision=target, hashes={'coordinator.py':hashlib.sha256((generation/'coordinator.py').read_bytes()).hexdigest()})))
                return generation
        self.Engine = Engine
        self.old_module = types.SimpleNamespace(Activator=Engine, switch=a.switch,atomic=a.atomic,SERVICES=a.SERVICES)
        self.new_module = types.SimpleNamespace(Activator=lambda *args,**kwargs:Engine(*args,**kwargs,role='new'),switch=a.switch,atomic=a.atomic,SERVICES=a.SERVICES)
        self.new_loaded = []
        def load(path,name):
            if name=='_installed_activation_supervisor':return self.old_module
            self.events.append('load-new')
            self.new_loaded.append((Path(path),Path(path).read_bytes()))
            self.assertTrue(Path(path).is_relative_to(self.state))
            return self.new_module
        self.loading=patch.object(u,'load_module',side_effect=load);self.loading.start();self.addCleanup(self.loading.stop)

    def git(self,*args):return a.run(['git',*args],self.repo)
    def commit(self,message):self.git('add','.');self.git('commit','-qm',message)
    def upgrade(self):return u.upgrade('p',self.source,self.home)
    def receipt(self):return json.loads((self.state/'activation-supervisor-upgrade.json').read_text())

    def real_individual_review(self):
        """Use the actual legacy verifier, actual merge parents and SQLite proof."""
        head = self.target
        self.git('branch', 'reviewed-feature', head)
        self.git('reset', '--hard', self.base)
        self.git('merge', '--no-ff', '-qm', 'reviewed individual integration', 'reviewed-feature')
        self.target = self.git('rev-parse', 'HEAD')
        db = sqlite3.connect(self.state/'reviews.sqlite3');self.addCleanup(db.close)
        db.executescript('CREATE TABLE activation_merges(project,pr,head,base,commit_id,review_id); CREATE TABLE jobs(id,head,status,publication_done,result,kind);')
        db.execute('INSERT INTO activation_merges VALUES(?,?,?,?,?,?)', ('p',1,head,self.base,self.target,3))
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (3,head,'done',1,json.dumps(dict(verdict='approve',acceptance_complete=True)),'review'))
        db.commit()
        remote = dict(merged_at='now',merge_commit_sha=self.target,
                      base=dict(ref='main',repo=dict(full_name='owner/repo')),
                      head=dict(sha=head,repo=dict(full_name='owner/repo')))
        self.Engine.gh = lambda engine, endpoint:remote
        fixture=self
        def verify(engine,old,target):
            fixture.events.append(engine.role+'-verify')
            return a.Activator.verify(engine,old,target)
        self.Engine.verify = verify
        return db

    def test_actual_installed_verifier_authorizes_reviewed_individual_merge(self):
        self.real_individual_review()
        self.assertEqual(self.upgrade()['installed'],self.target)
        self.assertLess(self.events.index('old-verify'),self.events.index('load-new'))

    def test_actual_installed_verifier_rejects_unpublished_review_before_import(self):
        db=self.real_individual_review()
        db.execute('UPDATE jobs SET publication_done=0');db.commit()
        with self.assertRaisesRegex(RuntimeError,'review unavailable'):self.upgrade()
        self.assertFalse(self.new_loaded)
        self.assertFalse(self.services.stops)

    def test_reviewed_main_upgrade_preserves_private_configuration_and_readiness(self):
        result=self.upgrade()
        self.assertEqual(result['installed'],self.target)
        self.assertEqual(result['supervisor'],'upgraded')
        self.assertEqual((self.library/'current').resolve(),self.library/'generations/new')
        self.assertEqual(self.supervisor.read_bytes(),b'# reviewed supervisor upgrade\n')
        self.assertEqual(self.dashboard.read_bytes(),b'# reviewed dashboard.py\n')
        self.assertEqual(self.private.read_bytes(),self.private_bytes)
        self.assertEqual(json.loads((self.config_dir/'activation.json').read_text()),self.settings)
        self.assertLess(self.events.index('old-verify'),self.events.index('load-new'))
        self.assertEqual(self.events[:4],['old-verify','load-new','new-verify','validate'])
        self.assertEqual(self.receipt()['phase'],'installed')
        self.assertEqual(json.loads((self.state/'activation.json').read_text())['phase'],'installed')
        self.assertEqual(self.services.stops,[list(a.SERVICES)])
        self.assertEqual(self.services.starts,[(list(a.SERVICES),str(self.library/'generations/new'))])

    def test_source_head_not_main_or_dirty_file_fails_before_new_code_or_stop(self):
        self.git('checkout','-qb','unmerged')
        (self.source/'worker.py').write_text('# unmerged code\n');self.commit('unmerged')
        with self.assertRaisesRegex(RuntimeError,'not fetched main'):self.upgrade()
        self.assertFalse(self.new_loaded);self.assertFalse(self.services.stops)
        self.git('checkout','-q',self.main)
        (self.source/'install.py').write_text('# dirty installer\n')
        with self.assertRaisesRegex(RuntimeError,'differs from committed main'):self.upgrade()
        self.assertFalse(self.new_loaded);self.assertFalse(self.services.stops)

    def test_untracked_helper_or_symlink_fails_before_import(self):
        extra=self.source/'dataclasses.py';extra.write_text('# shadow imported module\n')
        with self.assertRaisesRegex(RuntimeError,'extra file'):self.upgrade()
        extra.unlink();(self.source/'worker.py').unlink();(self.source/'worker.py').symlink_to(self.source/'coordinator.py')
        with self.assertRaisesRegex(RuntimeError,'regular committed files'):self.upgrade()
        self.assertFalse(self.new_loaded)

    def test_old_verifier_must_authorize_before_new_code(self):
        self.reject_old=True
        with self.assertRaisesRegex(RuntimeError,'old verifier rejected'):self.upgrade()
        self.assertFalse(self.new_loaded);self.assertFalse(self.services.stops)
        self.assertEqual((self.library/'current').resolve(),self.previous)

    def test_new_verifier_and_validation_fail_before_runtime_changes(self):
        self.reject_new=True
        with self.assertRaisesRegex(RuntimeError,'new verifier rejected'):self.upgrade()
        self.reject_new=False;self.reject_validation=True
        with self.assertRaisesRegex(RuntimeError,'offline validation'):self.upgrade()
        self.assertFalse(self.services.stops)
        self.assertEqual(self.supervisor.read_bytes(),self.old_bytes)
        self.assertEqual(self.dashboard.read_bytes(),self.old_dashboard)

    def test_source_mutation_after_validation_cannot_change_installed_bytes(self):
        self.mutate_source=True
        self.upgrade()
        self.assertEqual(self.new_loaded[0][1],b'# reviewed supervisor upgrade\n')
        self.assertEqual(self.supervisor.read_bytes(),b'# reviewed supervisor upgrade\n')
        self.assertEqual((self.source/'activation.py').read_bytes(),b'# dirty changed supervisor\n')

    def test_main_move_during_validation_keeps_existing_supervisor(self):
        self.move_tip=True
        with self.assertRaisesRegex(RuntimeError,'main moved'):self.upgrade()
        self.assertFalse(self.services.stops)
        self.assertEqual((self.library/'current').resolve(),self.previous)
        self.assertEqual(self.supervisor.read_bytes(),self.old_bytes)

    def test_failed_readiness_rolls_back_both_supervisor_and_generation(self):
        self.services.fail_start=True
        with self.assertRaisesRegex(RuntimeError,'startup readiness'):self.upgrade()
        self.assertEqual((self.library/'current').resolve(),self.previous)
        self.assertEqual(self.supervisor.read_bytes(),self.old_bytes)
        self.assertEqual(self.receipt()['phase'],'rolled_back')
        self.assertEqual(self.dashboard.read_bytes(),self.old_dashboard)
        self.assertEqual(json.loads((self.state/'activation.json').read_text())['phase'],'rolled_back')
        self.assertEqual(self.services.starts[-1],(list(a.SERVICES),str(self.previous)))
        with self.assertRaisesRegex(RuntimeError,'failed supervisor upgrade'):self.upgrade()

    def test_failed_supervisor_replace_restores_already_switched_link(self):
        original=u.replace_bytes
        def fail(path,data,mode=0o644):
            if path==self.supervisor and data!=self.old_bytes:raise OSError('replacement failed')
            return original(path,data,mode)
        with patch.object(u,'replace_bytes',side_effect=fail),self.assertRaisesRegex(OSError,'replacement failed'):self.upgrade()
        self.assertEqual((self.library/'current').resolve(),self.previous)
        self.assertEqual(self.supervisor.read_bytes(),self.old_bytes)
        self.assertEqual(self.receipt()['phase'],'rolled_back')

    def test_interrupted_pending_upgrade_recovers_once_without_reexecution(self):
        self.upgrade()
        receipt=self.receipt();receipt['phase']='pending'
        a.atomic(self.state/'activation-supervisor-upgrade.json',receipt)
        self.new_loaded.clear()
        with self.assertRaisesRegex(RuntimeError,'interrupted supervisor upgrade rolled back'):self.upgrade()
        self.assertFalse(self.new_loaded)
        self.assertEqual((self.library/'current').resolve(),self.previous)
        self.assertEqual(self.supervisor.read_bytes(),self.old_bytes)
        self.assertEqual(self.receipt()['phase'],'rolled_back')
        self.assertEqual(self.dashboard.read_bytes(),self.old_dashboard)

    def test_dashboard_replace_failure_rolls_back_supervisor_and_link(self):
        original=u.replace_bytes
        def fail(path,data,mode=0o644):
            if path==self.dashboard and data!=self.old_dashboard:raise OSError('dashboard replacement failed')
            return original(path,data,mode)
        with patch.object(u,'replace_bytes',side_effect=fail),self.assertRaisesRegex(OSError,'dashboard replacement failed'):self.upgrade()
        self.assertEqual((self.library/'current').resolve(),self.previous)
        self.assertEqual(self.supervisor.read_bytes(),self.old_bytes)
        self.assertEqual(self.dashboard.read_bytes(),self.old_dashboard)

    def test_normal_activation_refuses_an_interrupted_supervisor_upgrade(self):
        a.atomic(self.state/'activation-supervisor-upgrade.json',dict(phase='pending'))
        engine=a.Activator(self.settings,self.library,self.state,services=self.services)
        engine.tip=Mock()
        with self.assertRaisesRegex(RuntimeError,'supervisor upgrade is pending'):engine.activate()
        engine.tip.assert_not_called()

    def test_wrong_project_pending_normal_activation_and_corrupt_runtime_fail_closed(self):
        settings=self.config_dir/'activation.json';settings.write_text(json.dumps({**self.settings,'project':'other'}))
        with self.assertRaisesRegex(RuntimeError,'settings'):self.upgrade()
        settings.write_text(json.dumps(self.settings))
        a.atomic(self.state/'activation.json',dict(phase='pending'))
        with self.assertRaisesRegex(RuntimeError,'normal activation is pending'):self.upgrade()
        (self.state/'activation.json').unlink()
        (self.previous/'coordinator.py').write_text('# tampered installed runtime\n')
        with self.assertRaisesRegex(RuntimeError,'differs from its receipt'):self.upgrade()
        self.assertFalse(self.new_loaded)

    def test_inactive_parents_remain_inactive_and_serialization_is_real(self):
        self.services.active_units=[]
        import fcntl
        with (self.state/'activation.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.upgrade()
        self.upgrade()
        self.assertEqual(self.services.stops,[[]])
        self.assertEqual(self.services.starts,[([],str(self.library/'generations/new'))])


if __name__=='__main__':unittest.main()
