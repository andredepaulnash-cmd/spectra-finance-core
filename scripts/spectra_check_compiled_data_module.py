#!/usr/bin/env python3
"""Check a disposable installed wheel's data-module ABI without any connector."""
import argparse
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import re
import struct
import sys
import sysconfig
import zipfile

MANIFEST_SHA256 = 'c46ab17432c51297219eef692b93ab78f9c5450f22ad11f1677c448bb795ef77'
MODULE = 'hummingbot.core.data_type.order_book_query_result'
MEMBER = 'hummingbot/core/data_type/order_book_query_result.cpython-312-x86_64-linux-gnu.so'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--expected-wheel-sha256', required=True)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
        parser.error('Use the disposable Python 3.12 venv interpreter with -I -S -B')
    if sys.flags.optimize:
        parser.error('Optimized Python would omit checks; run without -O or -OO')
    # Python 3.12 -S leaves sys.prefix at the base interpreter. The executable
    # path still identifies the explicitly selected venv without running site,
    # .pth files or site customization. Add that venv first and then ordinary
    # base package directories explicitly; build venvs may inherit NumPy/pandas.
    venv = Path(sys.executable).absolute().parent.parent
    packages = venv / 'lib/python3.12/site-packages'
    if not (venv / 'pyvenv.cfg').is_file() or not packages.is_dir():
        parser.error('Use the disposable venv interpreter containing the built wheel')
    venv = venv.resolve()
    sys.path.append(str(packages))
    for key in ('purelib', 'platlib'):
        directory = sysconfig.get_path(key)
        if directory and directory not in sys.path:
            sys.path.append(directory)
    if not re.fullmatch('[0-9a-f]{64}', args.expected_wheel_sha256):
        parser.error('An independently recorded wheel checksum is required')
    manifest_bytes = args.manifest.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        parser.error('Exact reviewed PR33 overlay manifest is required')
    wheel_digest = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
    if wheel_digest != args.expected_wheel_sha256:
        parser.error('Wheel checksum mismatch')
    with zipfile.ZipFile(args.wheel) as archive:
        if archive.namelist().count(MEMBER) != 1:
            parser.error('Expected exactly one compiled data module in wheel')
        expected_module_digest = hashlib.sha256(archive.read(MEMBER)).hexdigest()
    def refuse_transport(event, arguments):
        if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
            raise RuntimeError('Transport and process launch are prohibited in this ABI check')
    sys.addaudithook(refuse_transport)
    distribution = importlib.metadata.distribution('hummingbot')
    files = []
    for row in json.loads(manifest_bytes)['files']:
        path = Path(distribution.locate_file(row['path'])).resolve()
        assert path.is_relative_to(venv), 'Expected disposable venv installation'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == row['sha256'], 'Installed patch checksum mismatch'
        files.append({'path': row['path'], 'sha256': digest})
    module = importlib.import_module(MODULE)
    path = Path(module.__file__).resolve()
    assert path.is_relative_to(venv), 'Module must come from installed disposable wheel'
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    assert digest == expected_module_digest, 'Installed compiled module differs from wheel'
    assert data[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', data, 18)[0] == 62
    value = module.OrderBookQueryResult(10.0, 2.0, 9.5, 1.5)
    assert (value.query_price, value.query_volume, value.result_price, value.result_volume) == (10.0, 2.0, 9.5, 1.5)
    assert not any(x.startswith('hummingbot.connector') for x in sys.modules)
    assert not any(x.startswith('hummingbot.strategy_v2') for x in sys.modules)
    print(json.dumps({
        'scope': 'one_compiled_data_module_abi_and_installed_patch_bytes',
        'wheel_sha256': wheel_digest, 'distribution_version': distribution.version,
        'python_abi': 'cp312', 'compiled_module': MODULE,
        'compiled_module_sha256': digest, 'elf_class': 64, 'elf_machine': 'x86_64',
        'data_class_round_trip_passed': True, 'five_installed_overlay_hashes_match': True,
        'overlay_files': files, 'network_and_process_audit_guard_enabled': True,
        'connector_imported': False, 'executor_imported': False, 'api_started': False,
        'full_installed_backend_guard_run': False, 'approved_for_live': False}, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
