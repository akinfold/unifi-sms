"""Ensure release promotion fails closed when validated image identities differ."""

import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / '.github/scripts/release-artifacts.py'
spec = importlib.util.spec_from_file_location('release_artifacts', MODULE_PATH)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseArtifactTests(unittest.TestCase):
    def create_archives(self, root):
        for architecture in release.PLATFORMS:
            folder = root / f'validated-image-{architecture}'
            folder.mkdir()
            archive = folder / 'image.tar'
            archive.write_bytes(f'archive-{architecture}'.encode())
            record = {
                'architecture': architecture, 'image_id': f'sha256:{architecture}',
                'archive_sha256': release.archive_digest(archive), 'labels': {'release': 'expected'},
            }
            (folder / 'validated.json').write_text(json.dumps(record))

    def test_load_retains_each_platform_by_validated_image_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_archives(root)
            with patch.object(release, 'expected_labels', return_value={'release': 'expected'}), \
                 patch.object(release, 'docker'), \
                 patch.object(release, 'image_details', side_effect=[
                     {'Id': 'sha256:amd64'}, {'Id': 'sha256:arm64'},
                 ]):
                self.assertEqual(release.load_candidates(root), {
                    'amd64': 'sha256:amd64', 'arm64': 'sha256:arm64',
                })

    def test_second_platform_identity_failure_prevents_all_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_archives(root)
            with patch.object(sys, 'argv', ['release-artifacts.py', 'promote', '--directory', directory]), \
                 patch.object(release, 'expected_labels', return_value={'release': 'expected'}), \
                 patch.object(release, 'docker'), \
                 patch.object(release, 'image_details', side_effect=[
                     {'Id': 'sha256:amd64'}, {'Id': 'sha256:unvalidated'},
                 ]), \
                 patch.object(release, 'promote') as promote:
                with self.assertRaisesRegex(RuntimeError, 'validated image ID'):
                    release.main()
                promote.assert_not_called()

    def test_changed_archive_is_rejected_before_loading(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_archives(root)
            (root / 'validated-image-amd64/image.tar').write_bytes(b'changed')
            with patch.object(release, 'expected_labels', return_value={'release': 'expected'}), \
                 patch.object(release, 'docker') as docker:
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    release.load_candidates(root)
                docker.assert_not_called()

    def test_release_metadata_mismatch_is_rejected_before_loading(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.create_archives(root)
            with patch.object(release, 'expected_labels', return_value={'release': 'another'}), \
                 patch.object(release, 'docker') as docker:
                with self.assertRaisesRegex(RuntimeError, 'requested release'):
                    release.load_candidates(root)
                docker.assert_not_called()

    def test_registry_config_must_match_both_validated_image_ids(self):
        index = {'annotations': {'org.opencontainers.image.description': 'Expected'}, 'manifests': [
            {'digest': 'sha256:a', 'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'digest': 'sha256:b', 'platform': {'os': 'linux', 'architecture': 'arm64'}},
        ]}
        with patch.object(release, 'description', return_value='Expected'), \
             patch.object(release, 'raw_manifest', side_effect=[index,
                 {'config': {'digest': 'sha256:amd64'}},
                 {'config': {'digest': 'sha256:unvalidated'}},
             ]):
            with self.assertRaisesRegex(RuntimeError, 'validated image IDs'):
                release.verify_index('example/image:1.0.0', {
                    'amd64': 'sha256:amd64', 'arm64': 'sha256:arm64',
                })


if __name__ == '__main__':
    unittest.main()
