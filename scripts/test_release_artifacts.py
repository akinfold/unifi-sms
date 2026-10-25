"""Ensure release promotion fails closed when validated image identities differ."""

import hashlib
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

    def test_oci_index_preserves_docker_child_descriptors_and_blobs(self):
        children = [
            {'mediaType': 'application/vnd.docker.distribution.manifest.v2+json',
             'digest': 'sha256:' + str(position) * 64, 'size': 123,
             'platform': {'os': 'linux', 'architecture': architecture}}
            for position, architecture in enumerate(release.PLATFORMS, 1)
        ]
        data = json.dumps({'schemaVersion': 2,
                           'mediaType': 'application/vnd.docker.distribution.manifest.list.v2+json',
                           'manifests': children}).encode()
        digest = hashlib.sha256(data).hexdigest()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            blobs = root / 'blobs' / 'sha256'
            blobs.mkdir(parents=True)
            (blobs / digest).write_bytes(data)
            (blobs / 'unchanged-layer').write_bytes(b'validated layer bytes')
            (root / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [
                {'digest': 'sha256:' + digest, 'size': len(data),
                 'mediaType': 'application/vnd.docker.distribution.manifest.list.v2+json'},
            ]}))
            with patch.object(release, 'description', return_value='README description'):
                reference = release.annotate_layout(root)
            descriptor = json.loads((root / 'index.json').read_text())['manifests'][0]
            converted = (blobs / descriptor['digest'].split(':')[1]).read_bytes()
            index = json.loads(converted)
            self.assertEqual(index['mediaType'], release.OCI_INDEX)
            self.assertEqual(index['annotations']['org.opencontainers.image.description'], 'README description')
            self.assertEqual(index['manifests'], children)
            self.assertEqual((blobs / digest).read_bytes(), data)
            self.assertEqual((blobs / 'unchanged-layer').read_bytes(), b'validated layer bytes')
            self.assertEqual(descriptor['digest'], 'sha256:' + hashlib.sha256(converted).hexdigest())
            self.assertEqual(descriptor['size'], len(converted))
            self.assertEqual(reference, f"oci-layout://{root}@{descriptor['digest']}")

    def test_changed_exported_index_is_rejected_before_annotation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = 'a' * 64
            blobs = root / 'blobs' / 'sha256'
            blobs.mkdir(parents=True)
            (blobs / digest).write_bytes(b'changed exported index')
            metadata = json.dumps({'manifests': [{'digest': 'sha256:' + digest}]})
            (root / 'index.json').write_text(metadata)
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                release.annotate_layout(root)
            self.assertEqual((root / 'index.json').read_text(), metadata)
            self.assertEqual(list(blobs.iterdir()), [blobs / digest])

    def test_publication_copies_one_annotated_index_without_recomposition(self):
        with patch.dict(release.os.environ, IMAGE_VERSION='0.1.6', RELEASE_SHA='validated'), \
             patch.object(release, 'docker') as docker, \
             patch.object(release, 'annotate_layout', return_value='oci-layout:///temporary@sha256:index'):
            self.assertEqual(release.publish_index('example/image', ['example/image@sha256:a', 'example/image@sha256:b']),
                             ('example/image:0.1.6', 'example/image:sha-validated'))
        calls = docker.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].args[4].startswith('oci-layout://'))
        self.assertEqual(calls[0].args[5:], ('example/image@sha256:a', 'example/image@sha256:b'))
        self.assertEqual(calls[1].args, ('buildx', 'imagetools', 'create', '--tag', 'example/image:0.1.6',
                                       '--tag', 'example/image:sha-validated', 'oci-layout:///temporary@sha256:index'))

    def test_docker_manifest_list_is_rejected_even_with_description(self):
        index = {'mediaType': 'application/vnd.docker.distribution.manifest.list.v2+json',
                 'annotations': {'org.opencontainers.image.description': 'Expected'}}
        with patch.object(release, 'raw_manifest', return_value=index):
            with self.assertRaisesRegex(RuntimeError, 'OCI media types'):
                release.verify_index('example/image:0.1.6', {})

    def test_registry_config_must_match_both_validated_image_ids(self):
        index = {'mediaType': release.OCI_INDEX, 'annotations': {'org.opencontainers.image.description': 'Expected'}, 'manifests': [
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
