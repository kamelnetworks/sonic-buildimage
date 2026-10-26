"""Exercise release workflow scripts without building images or contacting GitHub.

Run with: python3 -m unittest discover -s tests -p test_release_workflow.py
Requires PyYAML.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

WORKFLOW = yaml.safe_load(
    (Path(__file__).parents[1] / '.github/workflows/sonic-build.yml').read_text()
)
PLATFORMS = ['broadcom', 'marvell-teralynx', 'vs']


def script(job, name):
    return next(s['run'] for s in WORKFLOW['jobs'][job]['steps'] if s['name'] == name)


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, GITHUB_OUTPUT=str(self.root / 'outputs'))

    def run_script(self, text):
        return subprocess.run(
            ['bash', '-e', '-o', 'pipefail', '-c', text], cwd=self.root,
            env=self.env, text=True, capture_output=True,
        )

    def matrix(self, release, platforms='vs', targets=''):
        self.env.update(CREATE_RELEASE=str(release).lower(),
                        PLATFORMS_INPUT=platforms, TARGETS_INPUT=targets,
                        TARGETS_PLATFORM_INPUT='vs')
        result = self.run_script(script('setup', 'Build platform matrix'))
        if result.returncode:
            return result, None
        output = (self.root / 'outputs').read_text().strip().split('=', 1)[1]
        return result, json.loads(output)

    def test_release_forces_all_platforms(self):
        result, matrix = self.matrix(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(matrix, PLATFORMS)

    def test_release_rejects_component_targets(self):
        result, _ = self.matrix(True, targets='target/docker-teamd.gz')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'outputs').exists())

    def test_non_release_allows_single_platform(self):
        self.assertEqual(self.matrix(False)[1], ['vs'])

    def test_non_release_allows_component_targets(self):
        self.assertEqual(self.matrix(False, targets='target/docker-teamd.gz')[1], ['vs'])

    def prepare_assets(self):
        files = self.root / 'release-files'
        files.mkdir()
        for p in PLATFORMS:
            (files / f'{p}__uploaded-assets.txt').write_text(f'sonic-{p}.bin\nsonic-{p}.bin.log\n')
        self.env.update(RELEASE_PLATFORMS=' '.join(PLATFORMS), RELEASE_TAG='build-test-42')
        assets = ' '.join(f'sonic-{p}.bin sonic-{p}.bin.log' for p in PLATFORMS)
        self.env['MOCK_ASSETS'] = assets
        return files

    def verify(self):
        return self.run_script('gh() { echo "$MOCK_ASSETS"; }\n' + script('release', 'Verify release assets'))

    def test_complete_release_passes(self):
        self.prepare_assets()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_platform_manifest_blocks_release_even_with_stale_assets(self):
        files = self.prepare_assets()
        (files / 'vs__uploaded-assets.txt').unlink()
        self.assertNotEqual(self.verify().returncode, 0)

    def test_missing_installer_in_manifest_blocks_release(self):
        files = self.prepare_assets()
        (files / 'vs__uploaded-assets.txt').write_text('sonic-vs.bin.log\n')
        self.assertNotEqual(self.verify().returncode, 0)

    def test_missing_remote_asset_blocks_release(self):
        self.prepare_assets()
        self.env['MOCK_ASSETS'] = self.env['MOCK_ASSETS'].replace('sonic-vs.bin.log', '')
        self.assertNotEqual(self.verify().returncode, 0)

    def test_publish_preserves_branch_version_title(self):
        self.env.update(RELEASE_TAG='build-202611-master-42', RELEASE_TITLE='202611-master-v42')
        result = self.run_script('gh() { printf "%s\\n" "$@" >> gh-args; }\n' + script('release', 'Publish release'))
        self.assertEqual(result.returncode, 0, result.stderr)
        args = (self.root / 'gh-args').read_text().splitlines()
        self.assertEqual(args[:6], ['release', 'edit', 'build-202611-master-42',
                                   '--draft=false', '--title', '202611-master-v42'])

    def test_publish_requires_successful_build_matrix(self):
        release = WORKFLOW['jobs']['release']
        self.assertEqual(release['needs'], ['setup', 'build'])
        self.assertIn("needs.build.result == 'success'", release['if'])
        strategy = WORKFLOW['jobs']['build']['strategy']
        self.assertEqual(strategy['max-parallel'], 3)
        self.assertFalse(strategy['fail-fast'])


if __name__ == '__main__':
    unittest.main()
