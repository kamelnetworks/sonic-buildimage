import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'build-nova-sai'

class NovaBuildTests(unittest.TestCase):
    def test_help_does_not_fetch_or_build(self):
        result = subprocess.run([SCRIPT, '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('DOES NOT FORWARD PACKETS', result.stdout)

    def test_unknown_argument_is_rejected(self):
        self.assertEqual(subprocess.run([SCRIPT, '--install'], capture_output=True).returncode, 2)

    def test_local_source_builds_with_bazel_and_emits_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, debs, output = root / 'source', root / 'debs', root / 'out'
            (source / 'kernel').mkdir(parents=True)
            (source / 'MODULE.bazel').touch()
            (source / 'kernel/target.json').write_text('{}')
            debs.mkdir()
            bazel = root / 'bazel'
            bazel.write_text('''#!/usr/bin/env python3
from pathlib import Path
import sys
assert sys.argv[1:3] == ['build', '//packaging:deb']
assert sys.argv[3].startswith('--repo_env=NOVA_SAI_KERNEL_DEBS=')
Path('bazel-bin/packaging').mkdir(parents=True)
Path('bazel-bin/packaging/nova-sai_0.1.0_amd64.deb').write_bytes(b'test artifact')
''')
            bazel.chmod(0o755)
            subprocess.run([SCRIPT, '--source', source, '--kernel-debs', debs, '--output', output],
                           env=dict(os.environ, BAZEL=str(bazel)), check=True, capture_output=True)
            self.assertEqual((output / 'nova-sai_0.1.0_amd64.deb').read_bytes(), b'test artifact')
            self.assertIn('nova-sai_0.1.0_amd64.deb', (output / 'nova-sai_0.1.0_amd64.deb.sha256').read_text())

    def test_build_failure_does_not_publish_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / 'kernel').mkdir(parents=True)
            (source / 'MODULE.bazel').touch()
            (source / 'kernel/target.json').write_text('{}')
            output = root / 'out'
            result = subprocess.run([SCRIPT, '--source', source, '--kernel-debs', root, '--output', output],
                                    env=dict(os.environ, BAZEL='/bin/false'), capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

if __name__ == '__main__':
    unittest.main()
