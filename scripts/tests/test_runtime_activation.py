"""No-change activation can reuse validation; every uncertain input must go full."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('runtime_activation',ROOT/'scripts/runtime_activation.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class RuntimeActivationTest(unittest.TestCase):
    def setup_engine(self, directory):
        class Engine:
            def validate(self,target):
                self.validated.append(target)
                return 'full'
        engine=Engine();engine.link=Path(directory);engine.root=Path(directory);engine.validated=[]
        data={'coordinator.py':b'original\n','worker.py':b'worker\n'}
        for name,value in data.items():(engine.link/name).write_bytes(value)
        metadata={'protocol':1,'hashes':{name:hashlib.sha256(value).hexdigest() for name,value in data.items()}}
        (engine.link/'installed.json').write_text(json.dumps(metadata))
        module=SimpleNamespace(FILES=tuple(data),PREFIX='tools/pr-coordinator/',COMPATIBILITY={'activation_protocol':1},Activator=Engine,run=Mock())
        read=lambda root,target,name:data[Path(name).name]
        m.configure(module,read_source=read)
        return engine,module,data,metadata

    def test_identical_actual_metadata_and_target_skip_repeat_suite(self):
        with tempfile.TemporaryDirectory() as d:
            engine,_,_,_=self.setup_engine(d);engine.validate('a'*40)
            self.assertEqual(engine.validated,[])

    def test_changed_target_actual_metadata_protocol_or_missing_file_go_full(self):
        for kind in ['target','actual','metadata','protocol','missing','symlink','unreadable-target','json-type','missing-hash']:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                engine,module,data,metadata=self.setup_engine(d)
                if kind=='target':data['worker.py']=b'new runtime\n'
                if kind=='actual':(engine.link/'worker.py').write_bytes(b'tampered\n')
                if kind=='metadata':metadata['hashes']['worker.py']='0'*64
                if kind=='protocol':metadata['protocol']=True
                if kind in ('metadata','protocol'):(engine.link/'installed.json').write_text(json.dumps(metadata))
                if kind=='missing':(engine.link/'worker.py').unlink()
                if kind=='symlink':
                    other=engine.link/'original-worker';other.write_bytes(data['worker.py'])
                    (engine.link/'worker.py').unlink();(engine.link/'worker.py').symlink_to(other)
                if kind=='json-type':(engine.link/'installed.json').write_text('[]')
                if kind=='missing-hash':
                    del metadata['hashes']['worker.py'];(engine.link/'installed.json').write_text(json.dumps(metadata))
                if kind=='unreadable-target':data.clear()
                self.assertEqual(engine.validate('a'*40),'full');self.assertEqual(engine.validated,['a'*40])

    def test_only_full_suite_timeout_is_extended(self):
        spy=Mock()
        fake=SimpleNamespace(run=spy,Activator=type('E',(),{'validate':lambda self,t:None}))
        m.configure(fake)
        fake.run(['/usr/bin/python3','-m','unittest','discover','-s','tools/pr-coordinator/tests'],timeout=300)
        self.assertEqual(spy.call_args.kwargs['timeout'],900)
        fake.run(['git','show','HEAD'],timeout=120)
        self.assertEqual(spy.call_args.kwargs['timeout'],120)

if __name__=='__main__':unittest.main()
