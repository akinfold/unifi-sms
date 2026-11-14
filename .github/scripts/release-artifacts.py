"""Build, retain, and promote the container images validated by release CI."""

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from tempfile import TemporaryDirectory


PLATFORMS = ('amd64', 'arm64')
IMAGE = 'unifi-sms:ci'
OCI_INDEX = 'application/vnd.oci.image.index.v1+json'


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def docker(*args):
    return command('docker', *args)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def description():
    readme = Path('README.md').read_text()
    readme = re.sub(r'<!--.*?-->', '', readme, flags=re.S)
    paragraphs = re.split(r'\n\s*\n', readme.strip())
    paragraph = next((part for part in paragraphs if not part.lstrip().startswith('#')), '')
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', paragraph)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[`*_]', '', text)
    text = ' '.join(html.unescape(text).split())
    require(text and not any(ord(char) < 32 or ord(char) == 127 for char in text),
            'README.md must contain an introductory paragraph without control characters.')
    return text if len(text) <= 512 else text[:509].rsplit(' ', 1)[0].rstrip() + '...'


def expected_labels():
    return {
        'org.opencontainers.image.version': os.environ['IMAGE_VERSION'].removeprefix('v'),
        'org.opencontainers.image.revision': os.environ['RELEASE_SHA'],
        'org.opencontainers.image.source': 'https://github.com/' + os.environ['GITHUB_REPOSITORY'],
        'org.opencontainers.image.description': description(),
    }


def image_details(image, architecture):
    details = json.loads(docker('image', 'inspect', image))[0]
    require(details['Os'] == 'linux' and details['Architecture'] == architecture,
            f'Expected linux/{architecture}.')
    labels = details['Config']['Labels'] or {}
    require(all(labels.get(key) == value for key, value in expected_labels().items()),
            'Image labels differ from the release commit, version, or README description.')
    return details


def build(architecture):
    arguments = ['build', '--provenance=false', '--platform', f'linux/{architecture}', '--tag', IMAGE]
    for key, value in expected_labels().items():
        arguments.extend(['--label', f'{key}={value}'])
    subprocess.run(['docker', *arguments, '.'], check=True)
    image_details(IMAGE, architecture)


def archive_digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def archive_config_digest(archive, architecture):
    with tarfile.open(archive) as source:
        manifests = json.load(source.extractfile('manifest.json'))
        require(len(manifests) == 1, 'Expected one platform in the image archive.')
        config = source.extractfile(manifests[0]['Config']).read()
    details = json.loads(config)
    require(details.get('os') == 'linux' and details.get('architecture') == architecture,
            f'Expected linux/{architecture} in the image archive.')
    labels = details.get('config', {}).get('Labels') or {}
    require(all(labels.get(key) == value for key, value in expected_labels().items()),
            'Archived image labels differ from the requested release.')
    return 'sha256:' + hashlib.sha256(config).hexdigest()


def candidate_tag(architecture):
    return f'unifi-sms:validated-{architecture}'


def save_image(image, architecture, archive):
    docker('image', 'save', '--platform', f'linux/{architecture}', '--output', str(archive), image)
    return archive_config_digest(archive, architecture)


def export(architecture, directory):
    image_details(IMAGE, architecture)
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / 'image.tar'
    config_digest = save_image(IMAGE, architecture, archive)
    record = {
        'config_digest': config_digest, 'architecture': architecture,
        'archive_sha256': archive_digest(archive), 'labels': expected_labels(),
    }
    (directory / 'validated.json').write_text(json.dumps(record, indent=2) + '\n')


def load_candidates(directory):
    candidates = {}
    # Verify every archive before any registry write, including in publication jobs.
    for architecture in PLATFORMS:
        folder = directory / f'validated-image-{architecture}'
        record = json.loads((folder / 'validated.json').read_text())
        require(record['architecture'] == architecture and record['labels'] == expected_labels(),
                'Validated artifact metadata differs from the requested release.')
        archive = folder / 'image.tar'
        require(archive_digest(archive) == record['archive_sha256'], 'Image archive checksum differs.')
        config_digest = archive_config_digest(archive, architecture)
        require(config_digest == record['config_digest'], 'Archived image config differs from the validated config.')
        docker('image', 'load', '--input', str(archive))
        details = image_details(IMAGE, architecture)
        tag = candidate_tag(architecture)
        docker('image', 'tag', details['Id'], tag)
        # Docker's local ID may identify a manifest or index, depending on its image
        # store. Compare the raw config bytes after loading and retain a separate tag.
        with TemporaryDirectory(prefix='unifi-sms-loaded-image-') as temporary:
            loaded_digest = save_image(tag, architecture, Path(temporary) / 'image.tar')
        require(loaded_digest == config_digest, 'Loaded image config differs from the validated config.')
        candidates[architecture] = config_digest
    return candidates


def raw_manifest(reference):
    return json.loads(docker('buildx', 'imagetools', 'inspect', reference, '--raw'))


def manifest_digest(reference):
    return docker('buildx', 'imagetools', 'inspect', reference, '--format', '{{.Manifest.Digest}}')


def verify_index(reference, candidates):
    index = raw_manifest(reference)
    require(index.get('mediaType') == OCI_INDEX, 'Published index does not use OCI media types.')
    require(index.get('annotations', {}).get('org.opencontainers.image.description') == description(),
            'Published index description differs from README.')
    manifests = index.get('manifests', [])
    require(len(manifests) == len(PLATFORMS), 'Published index has unexpected manifests.')
    actual = {}
    repository = reference.split('@')[0].rsplit(':', 1)[0]
    for item in manifests:
        platform = item.get('platform', {})
        require(platform.get('os') == 'linux', 'Published manifest is not Linux.')
        architecture = platform.get('architecture')
        require(architecture in candidates and architecture not in actual,
                'Published manifest has an unexpected or duplicate platform.')
        manifest = raw_manifest(f"{repository}@{item['digest']}")
        actual[architecture] = manifest['config']['digest']
    require(actual == candidates, 'Published image configs differ from the validated config digests.')
    return manifest_digest(reference)


def annotate_layout(directory):
    root = directory / 'index.json'
    layout = json.loads(root.read_text())
    require(len(layout['manifests']) == 1, 'Expected one exported image index.')
    descriptor = layout['manifests'][0]
    algorithm, digest = descriptor['digest'].split(':', 1)
    require(algorithm == 'sha256' and re.fullmatch('[0-9a-f]{64}', digest),
            'Exported index has an invalid digest.')
    source = directory / 'blobs' / algorithm / digest
    require(archive_digest(source) == digest, 'Exported index checksum differs.')
    index = json.loads(source.read_bytes())
    require(len(index.get('manifests', [])) == len(PLATFORMS),
            'Exported index has unexpected manifests.')
    # Buildx drops annotations when every child uses Docker media types. Change only
    # the index envelope; child manifests, config IDs, and layer blobs stay intact.
    index['mediaType'] = OCI_INDEX
    index.setdefault('annotations', {})['org.opencontainers.image.description'] = description()
    data = json.dumps(index, separators=(',', ':'), sort_keys=True).encode()
    digest = hashlib.sha256(data).hexdigest()
    (directory / 'blobs' / 'sha256' / digest).write_bytes(data)
    descriptor.update(mediaType=OCI_INDEX, digest='sha256:' + digest, size=len(data))
    root.write_text(json.dumps(layout) + '\n')
    return f'oci-layout://{directory}@sha256:{digest}'


def publish_index(repository, sources):
    version = f"{repository}:{os.environ['IMAGE_VERSION']}"
    sha_tag = f"{repository}:sha-{os.environ['RELEASE_SHA']}"
    with TemporaryDirectory(prefix='unifi-sms-release-index-') as temporary:
        directory = Path(temporary) / 'image'
        docker('buildx', 'imagetools', 'create', '--tag', f'oci-layout://{directory}', *sources)
        source = annotate_layout(directory)
        # A single index source without --annotation is copied without recomposition.
        docker('buildx', 'imagetools', 'create', '--tag', version, '--tag', sha_tag, source)
    return version, sha_tag


def promote(candidates):
    digests = []
    for repository in (os.environ['GHCR_IMAGE'], os.environ['DOCKERHUB_IMAGE']):
        sources = []
        for architecture, config_digest in candidates.items():
            tag = f"{repository}:sha-{os.environ['RELEASE_SHA']}-{architecture}"
            docker('image', 'tag', candidate_tag(architecture), tag)
            subprocess.run(['docker', 'image', 'push', '--platform', f'linux/{architecture}', tag],
                           check=True)
            digest = manifest_digest(tag)
            reference = f'{repository}@{digest}'
            require(raw_manifest(reference)['config']['digest'] == config_digest,
                    'Pushed image config differs from the validated config digest.')
            sources.append(reference)
        version, sha_tag = publish_index(repository, sources)
        digest = verify_index(version, candidates)
        require(manifest_digest(sha_tag) == digest, 'Version and commit tags differ.')
        digests.append(digest)
    require(len(set(digests)) == 1, 'Registry release indexes differ.')
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(f'digest={digests[0]}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('build', 'export', 'verify', 'promote'))
    parser.add_argument('--architecture', choices=PLATFORMS)
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args()
    if args.operation in ('build', 'export'):
        require(args.architecture, '--architecture is required.')
    if args.operation != 'build':
        require(args.directory, '--directory is required.')
    if args.operation == 'build':
        build(args.architecture)
    elif args.operation == 'export':
        export(args.architecture, args.directory)
    else:
        candidates = load_candidates(args.directory)
        print('Verified both validated image archives, config digests, release labels, and platforms.')
        if args.operation == 'promote':
            promote(candidates)


if __name__ == '__main__':
    main()
