"""Read-only validator review using generated JSON files only.

These FENCED records are fictional test data, not registry updates or launches.
No replay, tape, historical loader, or strategy module is imported.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from research.weekly_reversal import launch_gate as launch


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class Packet:
    def __init__(self, root):
        self.root = root
        scope = dict(run_id='synthetic-run-1', attempt_id='synthetic-attempt-1',
                     arm='CONTROL_BBWP50_SOFT_RSI', input_sha256='1' * 64,
                     protocol_sha256='2' * 64, bounds=[0, 3600, 608400],
                     source_pins_sha256='3' * 64, rule_sha256='4' * 64,
                     output_directory=str((root / 'fictional-output').resolve()))
        self.expected = copy.deepcopy(scope)
        self.registry = dict(historical_execution_admitted=True,
                             old_campaign=dict(consumed=27, cap=28, paused=True),
                             new_campaign=dict(cap=60, currently_authorized_scoped_maximum=10,
                                               consumed=1, runs=[dict(scope, state='FENCED', consumed=True)]))
        self.admission = dict(scope, historical_execution_admitted=True, confirmed_launch_fence=True)
        self.confirmation = dict(state='DURABLE_PACKET_READBACK_CONFIRMED',
                                 library_file_id='libfile_' + 'a' * 32, library_version=1,
                                 packet_sha256='5' * 64)
        self.paths = {name:root / (name + '.json') for name in ('registry', 'admission', 'confirmation')}
        self.hashes = {}

    @property
    def run(self):
        return self.registry['new_campaign']['runs'][0]

    def save(self):
        self.paths['registry'].write_bytes(encoded(self.registry))
        self.hashes['registry'] = digest(self.paths['registry'].read_bytes())
        self.admission['registry_sha256'] = self.hashes['registry']
        self.paths['admission'].write_bytes(encoded(self.admission))
        self.hashes['admission'] = digest(self.paths['admission'].read_bytes())
        self.confirmation['registry_sha256'] = self.hashes['registry']
        self.confirmation['admission_sha256'] = self.hashes['admission']
        self.paths['confirmation'].write_bytes(encoded(self.confirmation))
        self.hashes['confirmation'] = digest(self.paths['confirmation'].read_bytes())
        return self

    def call(self):
        return launch.validate_launch_packet(
            admission_path=self.paths['admission'], expected_admission_sha256=self.hashes['admission'],
            registry_path=self.paths['registry'], confirmation_path=self.paths['confirmation'],
            expected_confirmation_sha256=self.hashes['confirmation'],
            expected_input_sha256=self.expected['input_sha256'],
            expected_protocol_sha256=self.expected['protocol_sha256'],
            expected_bounds=self.expected['bounds'],
            expected_source_pins_sha256=self.expected['source_pins_sha256'],
            expected_rule_sha256=self.expected['rule_sha256'],
            expected_output_directory=self.expected['output_directory'],
            expected_arm=self.expected['arm'])


@pytest.fixture
def packet(tmp_path):
    return Packet(tmp_path)


def test_exact_generated_packet_validates_read_only(packet):
    packet.save()
    before={name:path.read_bytes() for name,path in packet.paths.items()}
    assert packet.call() == packet.admission
    assert {name:path.read_bytes() for name,path in packet.paths.items()} == before
    assert not Path(packet.expected['output_directory']).exists()


@pytest.mark.parametrize('field,value', [
    ('input_sha256', '9'*64), ('protocol_sha256', '9'*64),
    ('source_pins_sha256', '9'*64), ('rule_sha256', '9'*64),
    ('bounds', [0, 3600, 608700]), ('arm', 'UNRECOGNIZED_ARM'),
    ('output_directory', '/fictional/wrong/output'), ('run_id', ''), ('attempt_id', ''),
    ('historical_execution_admitted', False), ('historical_execution_admitted', 1),
    ('confirmed_launch_fence', False), ('confirmed_launch_fence', 1),
])
def test_mismatched_admission_scope_rejected(packet,field,value):
    packet.admission[field]=value
    packet.run[field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field,value', [
    ('run_id', 'another-run'), ('attempt_id', 'another-attempt'),
    ('arm', 'LOOSE_BBWP20_SOFT_RSI'), ('input_sha256', '9'*64),
    ('protocol_sha256', '9'*64), ('bounds', [0, 3600, 608700]),
    ('source_pins_sha256', '9'*64), ('rule_sha256', '9'*64),
    ('output_directory', '/fictional/wrong/output'), ('consumed', False),
    ('state', 'UNCERTAIN'), ('state', 'RUNNING'), ('state', 'COMPLETED'), ('state', None),
])
def test_registry_run_must_bind_every_exact_admitted_field(packet,field,value):
    packet.run[field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('name', ['admission','registry','confirmation'])
def test_exact_authority_bytes_are_required(packet,name):
    packet.save()
    packet.paths[name].write_bytes(packet.paths[name].read_bytes()+b' ')
    with pytest.raises(ValueError, match='Authority bytes differ'): packet.call()


@pytest.mark.parametrize('field,value', [
    ('state', 'PENDING'), ('state', 'UNCERTAIN'), ('state', None),
    ('library_file_id', ''), ('library_file_id', 'arbitrary-id'),
    ('library_version', -1), ('library_version', 1.0), ('library_version', True),
    ('packet_sha256', ''), ('packet_sha256', '5'*63),
])
def test_missing_or_uncertain_publication_rejected(packet,field,value):
    packet.confirmation[field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field', ['registry_sha256','admission_sha256'])
def test_confirmation_binds_registry_and_admission_hashes(packet,field):
    packet.save()
    packet.confirmation[field]='9'*64
    raw=encoded(packet.confirmation)
    packet.paths['confirmation'].write_bytes(raw)
    packet.hashes['confirmation']=digest(raw)
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field,value', [
    ('consumed', 0), ('consumed', 2), ('consumed', 11), ('consumed', True), ('consumed', 1.0),
    ('cap', 59), ('currently_authorized_scoped_maximum', 11),
])
def test_changed_new_campaign_counts_rejected(packet,field,value):
    packet.registry['new_campaign'][field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field,value', [('consumed',26), ('cap',27), ('paused',False)])
def test_old_campaign_remains_separate_and_paused(packet,field,value):
    packet.registry['old_campaign'][field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_duplicate_run_ids_rejected(packet):
    packet.registry['new_campaign']['runs'].append(copy.deepcopy(packet.run))
    packet.registry['new_campaign']['consumed']=2
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_missing_attempt_id_rejected(packet):
    del packet.admission['attempt_id']
    del packet.run['attempt_id']
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('name', ['admission','registry','confirmation'])
@pytest.mark.parametrize('nested', [False,True])
def test_duplicate_json_keys_rejected(packet,name,nested):
    packet.save()
    raw=packet.paths[name].read_bytes()
    suffix=b',"extra":{"x":1,"x":2}}' if nested else b',"duplicate":1,"duplicate":2}'
    raw=raw[:-1]+suffix
    packet.paths[name].write_bytes(raw)
    packet.hashes[name]=digest(raw)
    if name=='registry':
        packet.admission['registry_sha256']=packet.hashes[name]
        packet.paths['admission'].write_bytes(encoded(packet.admission))
        packet.hashes['admission']=digest(packet.paths['admission'].read_bytes())
        packet.confirmation['registry_sha256']=packet.hashes[name]
        packet.confirmation['admission_sha256']=packet.hashes['admission']
    elif name=='admission':
        packet.confirmation['admission_sha256']=packet.hashes[name]
    if name!='confirmation':
        packet.paths['confirmation'].write_bytes(encoded(packet.confirmation))
        packet.hashes['confirmation']=digest(packet.paths['confirmation'].read_bytes())
    with pytest.raises(ValueError, match='Duplicate authority field'): packet.call()


@pytest.mark.parametrize('constant', ['NaN','Infinity','-Infinity'])
def test_nonfinite_json_constant_rejected(packet,constant):
    packet.save()
    raw=packet.paths['confirmation'].read_bytes()[:-1]+b',"extra":'+constant.encode()+b'}'
    packet.paths['confirmation'].write_bytes(raw)
    packet.hashes['confirmation']=digest(raw)
    with pytest.raises(ValueError, match='Nonfinite authority'): packet.call()


# Strict rejection regressions for the original reproduced gaps.
@pytest.mark.parametrize('field,value', [('consumed',27.0),('cap',28.0),('paused',1)])
def test_noninteger_old_campaign_rejected(packet,field,value):
    packet.registry['old_campaign'][field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field,value', [('cap',60.0),('currently_authorized_scoped_maximum',10.0)])
def test_noninteger_fixed_new_campaign_counts_rejected(packet,field,value):
    packet.registry['new_campaign'][field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_float_admission_bounds_rejected(packet):
    packet.admission['bounds']=[float(x) for x in packet.expected['bounds']]
    packet.run['bounds']=packet.admission['bounds']
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_alternate_valid_arm_must_match_selected_arm(packet):
    packet.admission['arm']=packet.run['arm']='LOOSE_BBWP20_SOFT_RSI'
    packet.save()
    assert packet.expected['arm']=='CONTROL_BBWP50_SOFT_RSI'
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field', ['run_id','attempt_id'])
def test_nonstring_identifiers_rejected(packet,field):
    packet.admission[field]=packet.run[field]=1
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('value', ['z'*64,['x']*64])
def test_invalid_packet_digest_rejected(packet,value):
    packet.confirmation['packet_sha256']=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_duplicate_attempt_ids_rejected(packet):
    another=copy.deepcopy(packet.run)
    another['run_id']='synthetic-run-2'
    another['output_directory']=str(packet.root/'another-fictional-output')
    packet.registry['new_campaign']['runs'].append(another)
    packet.registry['new_campaign']['consumed']=2
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_registry_float_bounds_rejected(packet):
    packet.run['bounds']=[float(x) for x in packet.expected['bounds']]
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_prior_run_without_attempt_id_rejected(packet):
    another=copy.deepcopy(packet.run)
    another['run_id']='synthetic-run-2'
    del another['attempt_id']
    another['output_directory']=str(packet.root/'another-fictional-output')
    packet.registry['new_campaign']['runs'].append(another)
    packet.registry['new_campaign']['consumed']=2
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('arm', sorted(launch.ARMS))
def test_both_allowed_arms_validate_only_when_expected(packet,arm):
    packet.expected['arm']=packet.admission['arm']=packet.run['arm']=arm
    packet.save()
    assert packet.call() == packet.admission


@pytest.mark.parametrize('count', [1,2,9,10])
def test_all_in_scope_counts_bind_unique_generated_attempts(packet,count):
    runs=packet.registry['new_campaign']['runs']
    for i in range(2,count+1):
        run=copy.deepcopy(packet.run)
        run['run_id']=f'synthetic-run-{i}'
        run['attempt_id']=f'synthetic-attempt-{i}'
        run['output_directory']=str(packet.root/f'fictional-output-{i}')
        runs.append(run)
    packet.registry['new_campaign']['consumed']=count
    packet.save()
    assert packet.call() == packet.admission


@pytest.mark.parametrize('field', ['state','library_file_id','library_version','packet_sha256'])
def test_missing_publication_fields_rejected(packet,field):
    del packet.confirmation[field]
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field', ['run_id','attempt_id','arm','input_sha256','protocol_sha256','bounds','source_pins_sha256','rule_sha256','output_directory','state','consumed'])
def test_missing_selected_attempt_fields_rejected(packet,field):
    del packet.run[field]
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('value', [[], None, 5, 'bad', [None], [{}], [{'run_id':1,'consumed':True}], [{'run_id':'x','consumed':1}]])
def test_malformed_run_collections_rejected(packet,value):
    packet.registry['new_campaign']['runs']=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('which', ['old_campaign','new_campaign'])
@pytest.mark.parametrize('value', [None,[],1,'bad'])
def test_malformed_campaign_objects_rejected(packet,which,value):
    packet.registry[which]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('name', ['registry','admission','confirmation'])
@pytest.mark.parametrize('value', [None,[],1,'bad'])
def test_top_level_authority_must_be_object(packet,name,value):
    packet.save()
    raw=encoded(value)
    packet.paths[name].write_bytes(raw)
    packet.hashes[name]=digest(raw)
    if name=='registry':
        packet.admission['registry_sha256']=packet.hashes[name]
        packet.paths['admission'].write_bytes(encoded(packet.admission))
        packet.hashes['admission']=digest(packet.paths['admission'].read_bytes())
        packet.confirmation['registry_sha256']=packet.hashes[name]
        packet.confirmation['admission_sha256']=packet.hashes['admission']
    elif name=='admission':
        packet.confirmation['admission_sha256']=packet.hashes[name]
    if name!='confirmation':
        packet.paths['confirmation'].write_bytes(encoded(packet.confirmation))
        packet.hashes['confirmation']=digest(packet.paths['confirmation'].read_bytes())
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field,value', [('bounds',[False,3600,608400]),('bounds',[0,3600.0,608400]),('arm','UNKNOWN')])
def test_invalid_expected_scope_rejected(packet,field,value):
    packet.expected[field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('field', ['input_sha256','protocol_sha256','source_pins_sha256','rule_sha256'])
@pytest.mark.parametrize('value', [None,'','z'*64,'1'*63])
def test_invalid_expected_identity_cannot_self_authorize(packet,field,value):
    packet.expected[field]=packet.admission[field]=packet.run[field]=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('value', [[],[0],[0,3600],[0,3600,608400,608700],[0,0,608400],[3600,0,608400],[-3600,0,604800]])
def test_invalid_expected_bounds_shape_or_order_rejected(packet,value):
    packet.expected['bounds']=packet.admission['bounds']=packet.run['bounds']=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('state', ['FENCED','RUNNING','CHECKPOINTED','COMPLETE','FAILED','UNKNOWN','STOPPED'])
def test_prior_lifecycle_attempts_must_remain_consumed(packet,state):
    prior=copy.deepcopy(packet.run)
    prior.update(run_id='synthetic-run-2',attempt_id='synthetic-attempt-2',state=state,consumed=False)
    packet.registry['new_campaign']['runs'].append(prior)
    # Deliberately omit it from the claimed consumption, as the original validator allowed.
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('state', ['FENCED','RUNNING','CHECKPOINTED','COMPLETE','FAILED','UNKNOWN','STOPPED'])
def test_prior_explicit_consumed_states_are_preserved_in_count(packet,state):
    prior=copy.deepcopy(packet.run)
    prior.update(run_id='synthetic-run-2',attempt_id='synthetic-attempt-2',state=state,consumed=True,
                 output_directory=str(packet.root/'fictional-prior-output'))
    packet.registry['new_campaign']['runs'].append(prior)
    packet.registry['new_campaign']['consumed']=2
    packet.save()
    assert packet.call()==packet.admission


@pytest.mark.parametrize('state', [None,'','UNCERTAIN','NOT_A_STATE'])
def test_prior_missing_or_unrecognized_state_rejected(packet,state):
    prior=copy.deepcopy(packet.run)
    prior.update(run_id='synthetic-run-2',attempt_id='synthetic-attempt-2',state=state)
    packet.registry['new_campaign']['runs'].append(prior)
    packet.registry['new_campaign']['consumed']=2
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_prior_absent_state_rejected(packet):
    prior=copy.deepcopy(packet.run)
    prior.update(run_id='synthetic-run-2',attempt_id='synthetic-attempt-2')
    del prior['state']
    packet.registry['new_campaign']['runs'].append(prior)
    packet.registry['new_campaign']['consumed']=2
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('value', [[0,1,604801],[0,3600,608401]])
def test_non_hour_aligned_expected_bounds_rejected(packet,value):
    packet.expected['bounds']=packet.admission['bounds']=packet.run['bounds']=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_eleven_consumed_attempts_exceed_scope_even_with_matching_count(packet):
    for i in range(2,12):
        prior=copy.deepcopy(packet.run)
        prior.update(run_id=f'synthetic-run-{i}',attempt_id=f'synthetic-attempt-{i}')
        packet.registry['new_campaign']['runs'].append(prior)
    packet.registry['new_campaign']['consumed']=11
    packet.save()
    with pytest.raises(ValueError): packet.call()


@pytest.mark.parametrize('value', [False,None,0,1,0.0,1.0,'true','false',[],{}])
def test_registry_global_admission_must_be_exactly_true(packet,value):
    packet.registry['historical_execution_admitted']=value
    packet.save()
    with pytest.raises(ValueError): packet.call()


def test_registry_global_admission_cannot_be_missing(packet):
    del packet.registry['historical_execution_admitted']
    packet.save()
    with pytest.raises(ValueError): packet.call()
