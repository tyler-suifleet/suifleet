"""SF-302: the daemon, Python SDK and TypeScript SDK agree on interfaces/ipc-v1.json.

Offline source checks only; real-daemon behavior is scripts/test_ipc.py and
scripts/test_ipc_node.py.
"""
import ast
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / 'interfaces/ipc-v1.json').read_text())


def strings(text):
    return re.findall(r'"([^"]*)"|\'([^\']*)\'', text)


def quoted(text):
    return [a or b for a, b in strings(text)]


class DaemonContract(unittest.TestCase):
    source = (ROOT / 'suifleet-edge/src/ipc.rs').read_text()

    def test_operations_match_daemon_order(self):
        block = re.search(r'const OPS: &\[&str\] = &\[(.*?)\];', self.source, re.S).group(1)
        self.assertEqual(quoted(block), CONTRACT['operations'])

    def test_administrator_only_operations(self):
        block = re.search(r'!\[(.*?)\]\.contains\(&op\.as_str\(\)\)', self.source).group(1)
        self.assertEqual(sorted(quoted(block)), sorted(CONTRACT['administrator_only']))

    def test_protocol_codes(self):
        for module, key in [('streams', 'stream'), ('shadows', 'shadow')]:
            text = (ROOT / f'suifleet-edge/src/{module}.rs').read_text()
            codes = set(re.findall(r'fail\(\s*"([a-z-]+)"', text)) | set(re.findall(r'code: "([a-z-]+)"', text))
            self.assertEqual(codes, set(CONTRACT['error_codes'][key]), module)

    def test_frame_limit(self):
        self.assertIn(f'MAX_FRAME: usize = {CONTRACT["max_frame_bytes"]}', (ROOT / 'suifleet-edge/src').joinpath('ipc.rs').read_text())


class PythonSdk(unittest.TestCase):
    source = (ROOT / 'sdk/python/suifleet_ipc.py').read_text()

    def constant(self, name):
        for node in ast.parse(self.source).body:
            if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == name for t in node.targets):
                return ast.literal_eval(node.value)
        raise AssertionError(name)

    def test_version_and_frame(self):
        self.assertEqual(self.constant('PROTOCOL_VERSION'), CONTRACT['protocol_version'])
        self.assertEqual(self.constant('MAX_FRAME'), CONTRACT['max_frame_bytes'])

    def test_cli_operations_are_protocol_operations(self):
        block = re.search(r"choices=\[(.*?)\]\)", self.source, re.S).group(1)
        self.assertLessEqual(set(quoted(block)), set(CONTRACT['operations']))

    def test_chunking_matches(self):
        self.assertIn(str(CONTRACT['limits']['data_put_chunk_bytes']), self.source)
        self.assertIn('64 * 1024 * 1024', self.source)


class TypeScriptSdk(unittest.TestCase):
    source = (ROOT / 'sdk/typescript/src/protocol.ts').read_text()

    def array(self, name):
        return quoted(re.search(rf'export const {name} = \[(.*?)\] as const;', self.source, re.S).group(1))

    def number(self, name):
        expression = re.search(rf'export const {name} = ([0-9 *]+?)(?: as const)?;', self.source).group(1)
        value = 1
        for factor in expression.split('*'):
            value *= int(factor)
        return value

    def test_operations(self):
        self.assertEqual(self.array('OPERATIONS'), CONTRACT['operations'])
        self.assertEqual(self.array('ADMINISTRATOR_OPERATIONS'), CONTRACT['administrator_only'])

    def test_constants(self):
        self.assertEqual(self.number('PROTOCOL_VERSION'), CONTRACT['protocol_version'])
        self.assertEqual(self.number('MAX_FRAME'), CONTRACT['max_frame_bytes'])
        self.assertEqual(self.number('MAX_PUBLISH_PAYLOAD'), CONTRACT['limits']['publish_payload_bytes'])
        self.assertEqual(self.number('DATA_CHUNK_SIZE'), CONTRACT['limits']['data_put_chunk_bytes'])
        self.assertEqual(self.number('MAX_DATA_SIZE'), CONTRACT['limits']['data_put_max_bytes'])

    def test_request_field_order(self):
        fields = re.search(r'JSON\.stringify\(\{\s*(.*?)\}\)', self.source, re.S).group(1)
        self.assertEqual(re.findall(r'(\w+)(?=[:,])', fields.replace('PROTOCOL_VERSION', ''))[:5], CONTRACT['request_fields'])

    def test_package_metadata(self):
        package = json.loads((ROOT / 'sdk/typescript/package.json').read_text())
        version = re.search(r"export const SDK_VERSION = '([^']+)';", self.source).group(1)
        self.assertEqual(package['version'], version)
        self.assertEqual(package['engines']['node'], '>=20.0.0')
        self.assertTrue(package['private'], 'publication to a public registry needs a reviewed release workflow')
        self.assertEqual(set(package['devDependencies']), {'typescript', '@types/node'})
        self.assertNotIn('dependencies', package, 'the runtime package has no third-party dependencies')
        self.assertTrue(all(re.fullmatch(r'\d+\.\d+\.\d+', v) for v in package['devDependencies'].values()), 'pin exact versions')


if __name__ == '__main__':
    unittest.main()
