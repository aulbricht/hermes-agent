"""R1 paired-contract attack. Mock only HTTP; execute the real Hermes dispatcher."""
import copy
import json

import httpx
import pytest

from model_tools import handle_function_call
from tools import atlas_ownership_tool as tool
from test_atlas_ownership_tool import CONTRACT, TOKEN, callback


def dispatch(request):
    return json.loads(handle_function_call('atlas_ownership_proposal', {
        'operation': request['operation'], **request['arguments']}))


@pytest.mark.parametrize('case', ['transformation_case', 'generic_case'])
@pytest.mark.parametrize('operation', ['propose', 'revise'])
@pytest.mark.parametrize('omitted', [None, 'depends_on', 'guidance_revision', 'catalog_revision', 'all'])
def test_semantic_ingress_matches_atlas_without_review_metadata(case, operation, omitted, callback):
    request = copy.deepcopy(CONTRACT[case]['requests'][operation == 'revise'])
    proposal = request['arguments']['proposal']
    if omitted == 'all':
        proposal.pop('changes', None)
    for item in proposal['transformations']:
        for key in ('depends_on', 'guidance_revision', 'catalog_revision'):
            if omitted in {'all', key}:
                item.pop(key, None)
    before = copy.deepcopy(request)
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        result = dispatch(request)
    finally:
        tool.clear_ownership_capability(bound)
    assert result == {'protocol': CONTRACT['protocol'], 'ok': True,
                      'operation': operation, 'result': {}}
    assert request == before
    assert len(callback[0]) == 1
    assert json.loads(callback[0][0].content) == before
    assert not tool.ownership_capability_available()


# These exact envelopes are produced by the Atlas attack tests, not inferred
# from a successful empty callback. Rules are validator-owned; no business text.
@pytest.mark.parametrize('code,rule', [
    ('OWNERSHIP_EVIDENCE_MISMATCH', 'plan_compiled_by_atlas'),
    ('OWNERSHIP_SHAPE_INVALID', 'record_targets_shape'),
    ('OWNERSHIP_TOO_LARGE', 'json_bytes_max_65536'),
])
@pytest.mark.parametrize('operation', ['propose', 'revise'])
def test_actual_atlas_rule_envelopes_survive_dispatch_and_a_same_turn_retry(code, rule, operation, callback, caplog):
    request = copy.deepcopy(CONTRACT['generic_case']['requests'][operation == 'revise'])
    envelope = {'protocol': CONTRACT['protocol'], 'ok': False, 'error': {'code': code, 'rule': rule}}
    callback[1][0] = lambda req: httpx.Response(200, json=envelope)
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert dispatch(request) == envelope
        callback[1][0] = lambda req: httpx.Response(200, json={
            'protocol': CONTRACT['protocol'], 'ok': True, 'operation': operation, 'result': {}})
        assert dispatch(request)['ok'] is True
    finally:
        tool.clear_ownership_capability(bound)
    assert len(callback[0]) == 2
    assert all(json.loads(r.content) == request for r in callback[0])
    assert TOKEN not in caplog.text


@pytest.mark.parametrize('error', [
    {'code': 'OWNERSHIP_SHAPE_INVALID', 'rule': 'Private value!'},
    {'code': 'OWNERSHIP_TOO_LARGE', 'rule': None},
    {'code': 'OWNERSHIP_UNAVAILABLE', 'rule': 'record_targets_shape'},
    {'code': 'OWNERSHIP_SHAPE_INVALID', 'rule': 'record_targets_shape', 'value': 'private'},
    {'code': 'OWNERSHIP_SHAPE_INVALID', 'rule': TOKEN},
])
def test_broadening_fixed_rule_support_does_not_forward_arbitrary_error_data(error, callback):
    request = copy.deepcopy(CONTRACT['generic_case']['requests'][0])
    callback[1][0] = lambda req: httpx.Response(200, json={
        'protocol': CONTRACT['protocol'], 'ok': False, 'error': error})
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert dispatch(request) == {'error': 'Ownership operation unavailable'}
    finally:
        tool.clear_ownership_capability(bound)
    assert len(callback[0]) == 1


@pytest.mark.parametrize('case', ['transformation_case', 'generic_case'])
def test_legacy_execution_authority_remains_refused_with_valid_record_targets(case, callback):
    request = copy.deepcopy(CONTRACT[case]['requests'][0])
    request['arguments']['proposal']['execution_plan'] = copy.deepcopy(
        CONTRACT['requests'][2]['arguments']['proposal']['execution_plan'])
    bound = tool.bind_ownership_capability(TOKEN)
    try:
        assert 'error' in dispatch(request)
    finally:
        tool.clear_ownership_capability(bound)
    assert callback[0] == []
