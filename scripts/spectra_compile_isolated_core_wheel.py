#!/usr/bin/env python3
"""Compile a disposable pinned/overlaid core checkout; never run its backend."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys

CORE_COMMIT = '9af100d6822da7d2d0291a906c730ef172284ee2'
CORE_TREE = '06c31cf98f8573c2f2a1c819bcfbcebe56d8ee2a'
MANIFEST_SHA256 = 'c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77'

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-checkout', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--wheel-dir', type=Path, required=True)
    parser.add_argument('--verify-source-only', action='store_true',
                        help='Check exact disposable input bytes without compiling or writing outputs')
    args = parser.parse_args()
    source, output = args.core_checkout.resolve(), args.wheel_dir.resolve()
    if source == output or source in output.parents or output in source.parents:
        parser.error('Use distinct disposable source and output directories')
    manifest_bytes = args.manifest.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        parser.error('Exact reviewed PR33 overlay manifest is required')
    manifest = json.loads(manifest_bytes)
    git = ['git', '-C', str(source), '-c', 'core.hooksPath=/dev/null',
           '-c', 'core.fsmonitor=false', '-c', 'core.fsmonitorHookVersion=2']
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                'GIT_TERMINAL_PROMPT': '0'})
    # No checkout, pull, fetch, remote URL inspection, credential helper or shell
    # command is used by these source identity reads.
    def read_git(*argv):
        return subprocess.check_output(git + list(argv), env=env,
                                       stderr=subprocess.DEVNULL, timeout=20).decode().strip()
    try:
        if read_git('rev-parse', 'HEAD') != CORE_COMMIT or read_git('rev-parse', 'HEAD^{tree}') != CORE_TREE:
            parser.error('Disposable checkout must have the exact reviewed core commit and tree')
        configuration = read_git('config', '--local', '--list')
        if any(line.lower().startswith(('filter.', 'include.', 'includeif.'))
               for line in configuration.splitlines()):
            parser.error('Disposable Git checkout must have no source filters or included configuration')
        # Compare physical source bytes against Git's pinned tree, rather than
        # diff/index state. assume-unchanged and skip-worktree cannot hide edits.
        pinned = {}
        for record in read_git('ls-tree', '-r', '-z', 'HEAD').split('\0'):
            if record:
                metadata, name = record.split('\t', 1)
                mode, kind, digest = metadata.split()
                if kind != 'blob' or mode not in ('100644', '100755'):
                    parser.error('Unsupported source tree entry; no symlink or submodule inputs')
                pinned[name] = (mode, digest)
        overlays = {row['path']: row for row in manifest['files']}
        for name, (mode, digest) in pinned.items():
            path = source / name
            for parent in path.parents:
                if parent == source:
                    break
                if parent.is_symlink():
                    parser.error('Source directory links are not accepted')
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                parser.error('Only ordinary pinned source files are accepted')
            if bool(info.st_mode & 0o111) != (mode == '100755'):
                parser.error('Pinned source executable mode mismatch')
            if name not in overlays:
                data = path.read_bytes()
                actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                if actual != digest:
                    parser.error('Physical tracked source differs from pinned Git tree')
        allowed = set(pinned) | set(overlays)
        # Include ignored files: old generated C++, bytecode or extra Python
        # files must not become hidden inputs to setup.py or package discovery.
        def refused_walk(error):
            raise error
        for directory, directories, files in os.walk(source, followlinks=False, onerror=refused_walk):
            if Path(directory) == source:
                directories[:] = [name for name in directories if name != '.git']
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    parser.error('Source directory links are not accepted')
            for name in files:
                relative = (Path(directory) / name).relative_to(source).as_posix()
                if relative not in allowed:
                    parser.error('Unknown, ignored or prior build input requires a fresh disposable checkout')
        for row in manifest['files']:
            path = source / row['path']
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
                parser.error('Overlaid source hash mismatch')
    except (OSError, subprocess.SubprocessError):
        parser.error('Required disposable source evidence unavailable')
    if args.verify_source_only:
        print(json.dumps({'scope': 'physical_pinned_core_source_preflight',
                          'core_commit': CORE_COMMIT, 'core_tree': CORE_TREE,
                          'pinned_source_files_verified': len(pinned),
                          'overlay_files_verified': len(manifest['files']),
                          'ignored_or_extra_inputs_admitted': False,
                          'compiled_by_this_invocation': False, 'approved_for_live': False}))
        return 0
    output.mkdir(parents=True, exist_ok=True)
    if list(output.glob('hummingbot-*.whl')):
        parser.error('Use an output directory with no existing Hummingbot wheel')
    os.chdir(source)
    # Resource control is confined to this build process. Upstream setup.py
    # stays byte-identical and its Cython generation sees two workers.
    os.cpu_count = lambda: 2
    sys.argv = ['setup.py', 'bdist_wheel', '--dist-dir', str(output)]
    runpy.run_path(str(source / 'setup.py'), run_name='__main__')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
