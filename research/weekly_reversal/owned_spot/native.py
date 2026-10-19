"""PR40-pinned native primitives for the new, isolated weekly paper adapter."""
import contextlib
import hashlib
import importlib
import json
from pathlib import Path
import threading
import types

WORKSPACE = Path(__file__).resolve().parents[3]
PINS = Path(__file__).with_name('native_source_pins.json')
_LOCK = threading.RLock()

def verify_sources():
    pins = json.loads(PINS.read_text())
    for item in pins:
        if hashlib.sha256((WORKSPACE/item['path']).read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError('PR40 native source identity mismatch: '+item['path'])
    return pins

verify_sources()
for name in ('models','capital_governor','economic_position','fill_reconciliation',
             'executor_provenance_bridge','accounting_health','vcap_state','vcap_lifecycle',
             'vcap_profit_exit','economic_exit','asset_fingerprint','kraken_market_fabric',
             'recovery_fingerprint'):
    module = importlib.import_module('spectra_core.'+name)
    if Path(module.__file__).resolve() != (WORKSPACE/'spectra_core'/f'{name}.py').resolve():
        raise RuntimeError('Native import outside the pinned repository')
    globals()[name] = module

@contextlib.contextmanager
def paper_time(now):
    with _LOCK:
        original = capital_governor.time
        capital_governor.time = types.SimpleNamespace(time=lambda:float(now))
        try:
            yield
        finally:
            capital_governor.time = original
