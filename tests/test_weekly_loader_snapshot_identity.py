"""Generated-file loader integrity check; no strategy or historical evaluation."""
import hashlib
from decimal import Decimal
import numpy as np
from research.weekly_reversal.causal_tape import load_admitted_tape


def test_admitted_generated_file_bytes_cannot_change_before_caller_seal(tmp_path):
    data=np.tile(np.array([10.,10.,10.,10.,100.,1.]),(36,1,1))
    path=tmp_path/'generated_only.npy';np.save(path,data)
    admitted=hashlib.sha256(path.read_bytes()).hexdigest()
    receipt=dict(historical_execution_admitted=True,confirmed_launch_fence=True,
                 input_sha256=admitted,protocol_sha256='unit-loader-only',run_id='no-replay')
    tape=load_admitted_tape(path,expected_sha256=admitted,admission=receipt,
                           data_start=0,warmup_start=3600,start=7200,end=10800,roster=('BTC',))
    changed=np.load(path,mmap_mode='r+');changed[24,0,:4]=20.;changed.flush();del changed
    try:
        tape.seal()
    except ValueError:
        return  # Rejecting changed source bytes also satisfies the invariant.
    assert tape.boundary(7200).observed_opens[0]==Decimal('10'), \
        'Admitted source hash labels a later mutable mmap generation'
