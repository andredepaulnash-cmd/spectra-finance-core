"""Read-only exact fence validation for the weekly coordinator.

This validates a receipt supplied by the campaign coordinator. It cannot verify
external Library publication or user permission by itself. No constructor, data
loader or historical strategy is invoked by validation.
"""
from pathlib import Path
import hashlib
import json
import re

ARMS = {'CONTROL_BBWP50_SOFT_RSI', 'LOOSE_BBWP20_SOFT_RSI'}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate authority field')
        result[key] = value
    return result


def _read(path, expected):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Authority bytes differ: ' + str(path))
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite authority')))


def validate_launch_packet(*, admission_path, expected_admission_sha256,
                           registry_path, confirmation_path, expected_confirmation_sha256,
                           expected_input_sha256, expected_protocol_sha256,
                           expected_bounds, expected_source_pins_sha256,
                           expected_rule_sha256, expected_output_directory, expected_arm):
    """Fail closed on mismatched locally supplied, externally confirmed authority."""
    identities = (expected_admission_sha256, expected_confirmation_sha256,
                  expected_input_sha256, expected_protocol_sha256,
                  expected_source_pins_sha256, expected_rule_sha256)
    if any(not isinstance(value, str) or re.fullmatch(r'[a-f0-9]{64}', value) is None
           for value in identities):
        raise ValueError('Exact SHA-256 expected identities required')
    if (expected_arm not in ARMS or type(expected_bounds) not in (tuple, list)
            or len(expected_bounds) != 3 or any(type(x) is not int or x < 0 or x % 3600
                                               for x in expected_bounds)
            or not expected_bounds[0] < expected_bounds[1] < expected_bounds[2]):
        raise ValueError('Explicit exact expected arm and three ordered UTC-hour bounds required')
    admission = _read(admission_path, expected_admission_sha256)
    confirmation = _read(confirmation_path, expected_confirmation_sha256)
    if not isinstance(admission, dict) or not isinstance(confirmation, dict):
        raise ValueError('Authority records must be objects')
    if (confirmation.get('state') != 'DURABLE_PACKET_READBACK_CONFIRMED'
            or confirmation.get('admission_sha256') != expected_admission_sha256
            or not isinstance(confirmation.get('library_file_id'), str)
            or re.fullmatch(r'libfile_[a-f0-9]{32}', confirmation['library_file_id']) is None
            or type(confirmation.get('library_version')) is not int
            or confirmation['library_version'] < 0
            or not isinstance(confirmation.get('packet_sha256'), str)
            or re.fullmatch(r'[a-f0-9]{64}', confirmation['packet_sha256']) is None):
        raise ValueError('Exact durable publication confirmation required')
    if (admission.get('historical_execution_admitted') is not True
            or admission.get('confirmed_launch_fence') is not True
            or admission.get('arm') != expected_arm
            or admission.get('input_sha256') != expected_input_sha256
            or admission.get('protocol_sha256') != expected_protocol_sha256
            or type(admission.get('bounds')) is not list
            or any(type(x) is not int for x in admission['bounds'])
            or admission['bounds'] != list(expected_bounds)
            or admission.get('source_pins_sha256') != expected_source_pins_sha256
            or admission.get('rule_sha256') != expected_rule_sha256
            or admission.get('output_directory') != str(Path(expected_output_directory).resolve())
            or not isinstance(admission.get('run_id'), str) or not admission['run_id']
            or not isinstance(admission.get('attempt_id'), str) or not admission['attempt_id']):
        raise ValueError('Admitted run scope differs')
    registry_sha = admission.get('registry_sha256')
    if not isinstance(registry_sha, str) or re.fullmatch(r'[a-f0-9]{64}', registry_sha) is None:
        raise ValueError('Exact registry SHA-256 required')
    if confirmation.get('registry_sha256') != registry_sha:
        raise ValueError('Registry publication differs')
    registry = _read(registry_path, registry_sha)
    if not isinstance(registry, dict):
        raise ValueError('Registry must be an object')
    if registry.get('historical_execution_admitted') is not True:
        raise ValueError('Sole registry does not permit historical execution')
    old = registry.get('old_campaign', {})
    campaign = registry.get('new_campaign', {})
    if (not isinstance(old, dict) or not isinstance(campaign, dict)
            or type(old.get('consumed')) is not int or type(old.get('cap')) is not int
            or old.get('paused') is not True
            or type(campaign.get('cap')) is not int
            or type(campaign.get('currently_authorized_scoped_maximum')) is not int):
        raise ValueError('Exact typed campaign scope required')
    runs = campaign.get('runs', [])
    if (registry.get('old_campaign') != {'consumed': 27, 'cap': 28, 'paused': True}
            or campaign.get('cap') != 60
            or campaign.get('currently_authorized_scoped_maximum') != 10
            or type(campaign.get('consumed')) is not int
            or not 1 <= campaign['consumed'] <= 10
            or type(runs) is not list
            or any(not isinstance(run, dict) or not isinstance(run.get('run_id'), str)
                   or not run['run_id'] or run.get('consumed') is not True
                   or not isinstance(run.get('attempt_id'), str) or not run['attempt_id']
                   or run.get('state') not in {'FENCED', 'RUNNING', 'CHECKPOINTED', 'COMPLETE', 'FAILED', 'UNKNOWN', 'STOPPED'}
                   or type(run.get('bounds')) is not list
                   or any(type(x) is not int for x in run['bounds']) for run in runs)
            or len({run['run_id'] for run in runs}) != len(runs)
            or len({run['attempt_id'] for run in runs}) != len(runs)
            or len(runs) != campaign['consumed']):
        raise ValueError('Campaign authority or consumed count differs')
    matches = [run for run in runs if run.get('run_id') == admission['run_id']]
    if len(matches) != 1:
        raise ValueError('No unique admitted run in registry')
    run = matches[0]
    keys = ('run_id', 'attempt_id', 'arm', 'input_sha256', 'protocol_sha256',
            'bounds', 'source_pins_sha256', 'rule_sha256', 'output_directory')
    if (run.get('consumed') is not True or run.get('state') != 'FENCED'
            or any(run.get(key) != admission.get(key) for key in keys)):
        raise ValueError('Registry fence does not bind this exact run')
    return admission
