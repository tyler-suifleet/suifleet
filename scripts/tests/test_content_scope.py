"""Keep cosmetic CI cheap without allowing unknown or mixed code to skip it."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('content_scope', ROOT/'scripts/content_scope.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class ContentScopeTest(unittest.TestCase):
    def test_presentation_only_and_combined_documentation(self):
        self.assertEqual(m.classify(['site/src/assets/style.css', 'site/src/pages/index.html']),
                         dict(full=False, marketing=True, docs=False))
        self.assertEqual(m.classify(['README.md', 'docs/INSTALL.md']),
                         dict(full=False, marketing=False, docs=True))
        self.assertEqual(m.classify(['site/src/layout.html', 'docs/INSTALL.md']),
                         dict(full=False, marketing=True, docs=True))

    def test_code_config_policy_and_unknown_paths_always_keep_full_checks(self):
        for path in ['dapp/src/app/page.tsx', 'contracts/sources/device.move',
                     'suifleet-edge/src/main.rs', 'profiles/testnet/frontend.env',
                     'site/src/site.json', 'site/src/assets/guide.js', 'site/build.py',
                     'site/media/guide/sign-in.mp4', '.github/workflows/ci.yml',
                     'pnpm-lock.yaml', 'AGENTS.md', 'CLAUDE.md',
                     'docs/tracking/acceptance-policy.json', *m.FULL_EXCEPTIONS,
                     '/docs/a.md', 'docs/../contracts/x.md', './docs/a.md']:
            with self.subTest(path=path):
                self.assertTrue(m.classify(['site/src/assets/style.css', path])['full'])
        self.assertTrue(m.classify([])['full'])

    def test_invalid_or_missing_ranges_and_manual_events_are_full(self):
        for event in [{}, {'inputs': {}}, {'before':'0'*40,'after':'a'*40},
                      {'before':'HEAD~1','after':'a'*40}, {'pull_request':{}}]:
            self.assertTrue(m.event_scope(event)['full'])
        self.assertTrue(m.event_scope({'before':'b'*40,'after':'a'*40},
                        git=lambda *a,**kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1,'git')))['full'])

    def test_unverifiable_or_large_range_cannot_silently_pass_content_ci(self):
        event={'before':'b'*40,'after':'a'*40}
        for data in (b'', b'\xff\0', b'\0'.join(f'docs/{i}.md'.encode() for i in range(301))+b'\0'):
            with self.subTest(size=len(data)):
                result=m.event_scope(event,git=lambda *a,**kw:data)
                self.assertTrue(result['full']);self.assertFalse(result['verified'])

    def test_content_lane_retains_the_existing_security_scanner(self):
        ci=(ROOT/'.github/workflows/ci.yml').read_text()
        original=ci[ci.index('  security-dependencies:'):ci.index('  security-codeql:')].strip()
        light=(ROOT/'.github/workflows/content.yml').read_text()
        copied=light[light.index('  security-dependencies:'):]
        copied=copied.replace("    needs: scope\n    if: needs.scope.outputs.full == 'false'\n",'',1)
        self.assertEqual(original,copied.strip())
        self.assertNotIn('environment:',light)
        self.assertNotIn('secrets.',light)

    def test_git_rename_cannot_hide_removal_of_a_code_file(self):
        with tempfile.TemporaryDirectory() as d:
            def git(*args):
                return subprocess.check_output(['git',*args],cwd=d,stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.name','Test User');git('config','user.email','test@example.com')
            Path(d,'engine.py').write_text('print("original")\n');git('add','.');git('commit','-m','base');base=git('rev-parse','HEAD')
            Path(d,'site/src').mkdir(parents=True);git('mv','engine.py','site/src/page.html');git('commit','-m','move');head=git('rev-parse','HEAD')
            call=lambda args,**kw: subprocess.check_output(args,cwd=d,**kw)
            self.assertTrue(m.event_scope({'before':base,'after':head},git=call)['full'])
            Path(d,'site/src/page.html').write_text('updated\n');git('add','.');git('commit','-m','text');next_head=git('rev-parse','HEAD')
            self.assertFalse(m.event_scope({'pull_request':{'base':{'sha':head},'head':{'sha':next_head}}},git=call)['full'])

    def test_heavy_workflows_share_conservative_server_filters(self):
        for name in ['ci.yml','edge-platforms.yml','windows-runtime.yml','profiles.yml','static-site.yml']:
            text=(ROOT/'.github/workflows'/name).read_text()
            expected=['**', *m.IGNORED_PATTERNS, *m.FULL_EXCEPTIONS]
            for path in expected:
                self.assertEqual(text.count(f"      - '{path}'"),2,(name,path))
        # Wallet controllers and manual/scheduled testnet requests are unchanged.
        text=(ROOT/'.github/workflows/testnet.yml').read_text()
        self.assertIn('workflows: [CI]',text)
        self.assertIn('needs.queue-plan.outputs.run',text)


if __name__=='__main__': unittest.main()
