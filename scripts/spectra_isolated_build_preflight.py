#!/usr/bin/env python3
"""Read exact PR33 build input bytes and local tool presence; grant no authority."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys

PR33_COMMIT = 'fad5f320b2f87cc7c089bea2750dd8344aa1b661'
PR33_TREE = '6d15736a95b20bbd0e5aee8e34a07217f8143b91'
INPUTS_PATH = 'docs/build-results/core-wheel-20261010/pr33-source-inputs.json'
INPUTS_SHA256 = 'eaebfc15b483c1a14e9f72933a752d0a3f730801517ad51123621fc986bd1dd2'
MAX_FILE_BYTES = 2 * 1024 * 1024

def read_regular(root: Path, name: str) -> bytes:
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('unsafe relative path')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('symbolic links are not build inputs')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('build input is not a regular file')
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('build input is too large')
    return data

def collect(root: Path, base_image: str | None = None) -> dict:
    valid_base = bool(base_image and re.fullmatch(
        r'continuumio/miniconda3@sha256:[0-9a-f]{64}', base_image))
    result = {
        'schema_version': 1,
        'scope': 'static_isolated_build_preflight',
        'reviewed_source_commit': PR33_COMMIT,
        'reviewed_source_tree': PR33_TREE,
        'source_inputs_verified': False,
        'input_errors': [],
        'host': {'os_id': None, 'architecture': platform.machine(),
                 'python_version': platform.python_version()},
        'tool_presence': {name: shutil.which(name) is not None
                          for name in ('docker', 'podman', 'buildah', 'conda', 'gcc', 'g++', 'git')},
        'base_image_declaration': base_image if valid_base else None,
        'base_image_digest_format_valid': valid_base,
        'base_image_independently_reviewed': False,
        'daemon_and_buildkit_verified': False,
        'private_api_source_access_verified': False,
        'dependencies_fully_locked': False,
        'compiled_wheel_verified_by_this_preflight': False,
        'image_built_by_this_preflight': False,
        'installed_bazzite_verified': False,
        'production_modified': False,
        'venue_requests_performed': False,
        'approved_for_live': False,
    }
    try:
        result['host']['os_id'] = platform.freedesktop_os_release().get('ID')
    except OSError:
        pass
    try:
        raw = read_regular(root, INPUTS_PATH)
        if hashlib.sha256(raw).hexdigest() != INPUTS_SHA256:
            raise ValueError('reviewed input receipt checksum mismatch')
        receipt = json.loads(raw)
        if receipt['source_commit'] != PR33_COMMIT or receipt['source_tree'] != PR33_TREE:
            raise ValueError('reviewed input receipt identity mismatch')
        for row in receipt['files']:
            try:
                data = read_regular(root, row['path'])
                blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                if (blob != row['git_blob_sha'] or
                    hashlib.sha256(data).hexdigest() != row['sha256'] or
                    len(data) != row['bytes']):
                    raise ValueError('reviewed input byte mismatch')
            except (OSError, ValueError):
                result['input_errors'].append({'path': row['path'], 'reason': 'missing, unsafe or mismatched reviewed input'})
        result['source_inputs_verified'] = not result['input_errors']
        result['checked_source_files'] = len(receipt['files'])
    except (OSError, ValueError, KeyError, TypeError):
        result['input_errors'].append({'path': INPUTS_PATH, 'reason': 'missing, unsafe or mismatched reviewed input receipt'})
    result['build_status'] = 'NOT_RUN'
    result['remaining_requirements'] = [
        'A usable isolated Docker BuildKit builder.',
        'An independently reviewed immutable Miniconda base digest and intended platform.',
        'Separately supplied private API read authorization via a BuildKit secret.',
        'Successful exact-recipe image build and compiled installed-module guard.',
        'Review of resolved dependencies, wheel identity and image digest.',
        'Separate read-only evidence from the actual Bazzite host.',
    ]
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path('.'))
    parser.add_argument('--base-image')
    args = parser.parse_args()
    report = collect(args.source_root.resolve(), args.base_image)
    print(json.dumps(report, indent=2, sort_keys=True))
    # Success attests only this input byte comparison. Builder readiness,
    # compilation, installation and trading authority remain unverified.
    return 0 if report['source_inputs_verified'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
