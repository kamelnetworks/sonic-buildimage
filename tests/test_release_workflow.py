"""Exercise release workflow scripts without building images or contacting GitHub.

Run with: python3 -m unittest discover -s tests -p test_release_workflow.py
Requires PyYAML.
"""
import hashlib
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
RELEASE_HELPER = Path(__file__).parents[1] / 'scripts/release-assets.sh'


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

    def prepare_multipart_installer(self):
        files = self.prepare_assets()
        names = ['sonic-vs.bin.part-000', 'sonic-vs.bin.part-001']
        (files / 'vs__installer-parts.txt').write_text('\n'.join(names) + '\n')
        names += ['sonic-vs.bin.parts', 'sonic-vs.bin.sha256', 'sonic-vs.bin.log']
        (files / 'vs__uploaded-assets.txt').write_text('\n'.join(names) + '\n')
        self.env['MOCK_ASSETS'] = self.env['MOCK_ASSETS'].replace('sonic-vs.bin ', '') + ' ' + ' '.join(names)
        return files

    def test_complete_multipart_installer_passes(self):
        self.prepare_multipart_installer()
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_remote_part_blocks_release(self):
        self.prepare_multipart_installer()
        self.env['MOCK_ASSETS'] = self.env['MOCK_ASSETS'].replace('sonic-vs.bin.part-001', '')
        self.assertNotEqual(self.verify().returncode, 0)

    def test_missing_part_from_upload_manifest_blocks_release(self):
        files = self.prepare_multipart_installer()
        manifest = files / 'vs__uploaded-assets.txt'
        manifest.write_text(manifest.read_text().replace('sonic-vs.bin.part-001\n', ''))
        self.assertNotEqual(self.verify().returncode, 0)

    def test_missing_reassembly_metadata_blocks_release(self):
        files = self.prepare_multipart_installer()
        manifest = files / 'vs__uploaded-assets.txt'
        manifest.write_text(manifest.read_text().replace('sonic-vs.bin.sha256\n', ''))
        self.assertNotEqual(self.verify().returncode, 0)

    def test_missing_parts_manifest_blocks_release(self):
        files = self.prepare_multipart_installer()
        (files / 'vs__installer-parts.txt').unlink()
        self.assertNotEqual(self.verify().returncode, 0)

    def test_out_of_order_parts_block_release(self):
        files = self.prepare_multipart_installer()
        (files / 'vs__installer-parts.txt').write_text('sonic-vs.bin.part-001\nsonic-vs.bin.part-000\n')
        self.assertNotEqual(self.verify().returncode, 0)

    def test_asset_split_boundary_and_byte_exact_reassembly(self):
        self.env['RELEASE_HELPER'] = str(RELEASE_HELPER)
        # Exercise the same splitting path at a small threshold, including
        # exactly-at-limit files, without allocating multi-gigabyte fixtures.
        for size in [63, 64, 65, 160]:
            with self.subTest(size=size):
                data = bytes(range(size))
                (self.root / 'input').write_bytes(data)
                name = f'installer-{size}.bin'
                self.env['ASSET_NAME'] = name
                result = self.run_script('source "$RELEASE_HELPER"; prepare_release_asset input "$ASSET_NAME" 64')
                self.assertEqual(result.returncode, 0, result.stderr)
                assets = self.root / 'assets'
                if size < 64:
                    self.assertEqual((assets / name).read_bytes(), data)
                    continue
                names = (assets / f'{name}.parts').read_text().splitlines()
                self.assertGreaterEqual(len(names), 2)
                self.assertTrue(all((assets / part).stat().st_size < 64 for part in names))
                reassembled = b''.join((assets / part).read_bytes() for part in names)
                self.assertEqual(reassembled, data)
                self.assertEqual((assets / f'{name}.sha256').read_text(),
                                 f'{hashlib.sha256(data).hexdigest()}  {name}\n')
                uploaded = (self.root / 'uploaded-assets.txt').read_text().splitlines()
                self.assertTrue(set(names + [f'{name}.parts', f'{name}.sha256']).issubset(uploaded))

    def test_upload_stages_installer_log_and_renamed_debug_image(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        (scripts / 'release-assets.sh').write_text(RELEASE_HELPER.read_text())
        target = self.root / 'target'
        target.mkdir()
        for name in ['sonic-vs.bin', 'sonic-vs.bin.log', 'docker-syncd-dbg.gz']:
            (target / name).write_bytes(b'asset data')
        self.env.update(ALREADY_PUBLISHED='false', MATRIX_PLATFORM='vs', TARGETS_INPUT='', RELEASE_TAG='build-test-42')
        result = self.run_script('gh() { printf "%s\\n" "$*" >> gh-args; }\n' + script('build', 'Upload release assets to the draft'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = ['sonic-vs.bin', 'sonic-vs.bin.log', 'docker-vs-syncd-dbg.tar.gz']
        self.assertEqual((self.root / 'uploaded-assets.txt').read_text().splitlines(), expected)
        calls = (self.root / 'gh-args').read_text()
        for name in expected:
            self.assertIn(f'release upload build-test-42 assets/{name} --clobber', calls)

    def stage_nonrelease(self, targets='', names=None):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        (scripts / 'release-assets.sh').write_text(RELEASE_HELPER.read_text())
        for name in names or []:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'binary output')
        self.env.update(MATRIX_PLATFORM='vs', TARGETS_INPUT=targets)
        return self.run_script(script('build', 'Stage non-release binaries'))

    def test_nonrelease_preserves_component_outputs(self):
        names = ['target/debs/trixie/module.deb', 'target/python-wheels/trixie/api.whl']
        result = self.stage_nonrelease(' '.join(names), names)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in names:
            output = self.root / 'artifact-root' / ('vs__' + Path(name).name)
            self.assertEqual(output.read_bytes(), b'binary output')

    def test_nonrelease_preserves_default_installer(self):
        result = self.stage_nonrelease(names=['target/sonic-vs.bin'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'artifact-root/vs__sonic-vs.bin').read_bytes(), b'binary output')

    def test_nonrelease_missing_output_fails(self):
        self.assertNotEqual(self.stage_nonrelease('target/missing.deb').returncode, 0)

    def test_nonrelease_empty_target_list_fails(self):
        self.assertNotEqual(self.stage_nonrelease('  ').returncode, 0)

    def test_nonrelease_duplicate_basenames_fail(self):
        names = ['target/debs/trixie/module.deb', 'target/debs/bookworm/module.deb']
        self.assertNotEqual(self.stage_nonrelease(' '.join(names), names).returncode, 0)

    def test_nonrelease_staging_does_not_publish(self):
        step = next(s for s in WORKFLOW['jobs']['build']['steps']
                    if s.get('name') == 'Stage non-release binaries')
        self.assertIn('!inputs.create_release', step['if'])
        self.assertIn('success()', step['if'])
        self.assertNotIn('gh release', step['run'])

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
