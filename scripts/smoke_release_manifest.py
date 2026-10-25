"""Verify OCI release metadata with Docker-format children in an isolated registry."""

import argparse
import hashlib
from http.client import HTTPException
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import time
from urllib.error import URLError
from urllib.request import urlopen


REGISTRY_IMAGE = 'registry:3@sha256:01cd312e67026f06f5d89c3e133a4a9adc7cb9eeb9b1194e0b2954dfd770efd2'
CONTAINER = 'unifi-sms-manifest-registry'


def docker(*arguments):
    return subprocess.check_output(['docker', *arguments], text=True, timeout=120).strip()


def load_fixture(directory, architecture):
    config = json.dumps({'architecture': architecture, 'os': 'linux', 'config': {},
                         'rootfs': {'type': 'layers', 'diff_ids': []}}).encode()
    digest = hashlib.sha256(config).hexdigest()
    config_name = digest + '.json'
    tag = f'unifi-sms-manifest-smoke:{architecture}'
    manifest = json.dumps([{'Config': config_name, 'RepoTags': [tag], 'Layers': []}]).encode()
    archive = directory / (architecture + '.tar')
    with tarfile.open(archive, 'w') as output:
        for name, data in ((config_name, config), ('manifest.json', manifest)):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            output.addfile(entry, io.BytesIO(data))
    docker('image', 'load', '--input', str(archive))
    return tag, 'sha256:' + digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5108)
    parser.add_argument('--helper', type=Path, default=Path('.github/scripts/release-artifacts.py'))
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('release_artifacts', args.helper)
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    tags = []
    started = False
    try:
        docker('run', '--detach', '--name', CONTAINER, '--publish', f'127.0.0.1:{args.port}:5000', REGISTRY_IMAGE)
        started = True
        port = args.port
        repository = f'localhost:{port}/unifi-sms'
        deadline = time.monotonic() + 20
        while True:
            try:
                with urlopen(f'http://127.0.0.1:{port}/v2/', timeout=1) as response:
                    assert response.status == 200
                break
            except (URLError, TimeoutError, HTTPException):
                if time.monotonic() >= deadline:
                    raise RuntimeError('The isolated test registry did not start.') from None
                time.sleep(0.2)
        with TemporaryDirectory(prefix='unifi-sms-manifest-smoke-') as temporary:
            fixtures = {arch: load_fixture(Path(temporary), arch) for arch in release.PLATFORMS}
            tags.extend(tag for tag, _ in fixtures.values())
            candidates = {arch: image_id for arch, (_, image_id) in fixtures.items()}
            os.environ.update(IMAGE_VERSION='0.0.0', RELEASE_SHA='a' * 40)
            indexes = []
            for suffix in ('ghcr', 'dockerhub'):
                target = f'{repository}-{suffix}'
                sources = []
                for arch, (fixture, _) in fixtures.items():
                    tag = f'{target}:{arch}'
                    tags.append(tag)
                    docker('image', 'tag', fixture, tag)
                    docker('image', 'push', '--platform', f'linux/{arch}', tag)
                    manifest = release.raw_manifest(tag)
                    assert manifest['mediaType'] == 'application/vnd.docker.distribution.manifest.v2+json'
                    assert manifest['config']['digest'] == candidates[arch]
                    sources.append(f'{target}@{release.manifest_digest(tag)}')
                version, sha_tag = release.publish_index(target, sources)
                digest = release.verify_index(version, candidates)
                assert release.manifest_digest(sha_tag) == digest
                indexes.append(digest)
            assert indexes[0] == indexes[1], 'Registry indexes differ.'
        print('Release manifest check passed: OCI description, both platforms, unchanged configs, identical registry digests.')
    finally:
        if started:
            docker('rm', '--force', CONTAINER)
        for tag in tags:
            subprocess.run(['docker', 'image', 'rm', tag], capture_output=True, timeout=30, check=False)


if __name__ == '__main__':
    main()
