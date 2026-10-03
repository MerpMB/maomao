import json
from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError
from merp_relay.contracts import *
from merp_relay.routing import route
from merp_relay.adapters import normalize, classify_error, CodexAdapter, ClaudeAdapter

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def task(**changes):
    return TaskRequest(task_id='t', objective='inspect', kind='INVESTIGATE',
        tools=frozenset({'read'}), capabilities=frozenset({'reason'}),
        completion_criteria=('cite source',), read_scope=('fixture',)).model_copy(update=changes)


def profile(resource='a', pool='A', **changes):
    return ResourceProfile(resource_id=resource, provider='synthetic', runtime='fixture',
        authentication_mode='subscription', capabilities=frozenset({'reason'}),
        tools=frozenset({'read'}), pool_id=pool, pool_mapping_source='test configuration',
        pool_mapping_evidence='design_hypothesis', revalidated_at=NOW).model_copy(update=changes)


def observation(pool='A', status='available', **changes):
    return ResourceObservation(observation_id=pool+'-obs', pool_id=pool, status=status,
        source='synthetic event', evidence='locally_observed', observed_at=NOW-timedelta(minutes=1),
        expires_at=NOW+timedelta(minutes=1), **changes)


def decide(t=None, profiles=None, observations=None, reserves=None, preference=None):
    return route(t or task(), profiles or [profile()], observations or [], reserves or [],
                 preference or ['a', 'b'], NOW, 'fixture-config-v1')


def test_missing_tool_even_available():
    d = decide(profiles=[profile(tools=frozenset())], observations=[observation()])
    assert d.outcome == 'blocked' and 'missing tools: read' in d.excluded['a']


@pytest.mark.parametrize('change', [{'tools': frozenset({'network'})},
                                    {'capabilities': frozenset({'vision'})}])
def test_requirements_change_eligibility(change):
    assert decide(observations=[observation()]).selected == 'a'
    assert decide(t=task(**change), observations=[observation()]).selected is None


def reserved(observations):
    return decide(profiles=[profile(), profile('b', 'B')], observations=observations,
        reserves=[ReservePolicy(pool_id='B', preferred_kind='REVIEW', release_when_exhausted='A')])


def test_reserve_normal_and_release():
    assert reserved([observation()]).eligible == ('a',)
    assert reserved([observation(status='exhausted')]).selected == 'b'


def test_unknown_quota_not_fabricated():
    o = observation(status='unknown')
    assert o.headroom is None and decide(observations=[o]).selected == 'a'
    with pytest.raises(ValidationError):
        observation(status='unknown', headroom=1, accounting_basis='percent')


def test_expired_exhaustion_unknown_not_release():
    o = observation(status='exhausted').model_copy(update={'expires_at': NOW})
    d = reserved([o])
    assert d.selected == 'a' and d.eligible == ('a',)
    assert o.observation_id in d.observation_refs


def test_shared_pool_exhausted():
    d = decide(profiles=[profile(), profile('alias', 'A')],
               observations=[observation(status='exhausted')])
    assert d.outcome == 'blocked' and len(d.excluded) == 2


def test_boundary_requires_enforcement():
    control = Control(state='observed_only', enforcer='runtime',
                      evidence='locally_observed', source='no changes')
    d = decide(t=task(boundary_requirements=frozenset({'no_writes'})),
               profiles=[profile(controls={'no_writes': control})])
    assert 'boundary not enforced: no_writes' in d.excluded['a']


def test_headroom_comparability_and_preference():
    profiles = [profile(), profile('b', 'B')]
    obs = [observation(headroom=.2, accounting_basis='same-window'),
           observation('B', headroom=.8, accounting_basis='same-window')]
    assert decide(profiles=profiles, observations=obs).selected == 'b'
    obs[1] = obs[1].model_copy(update={'accounting_basis': 'different-window'})
    assert decide(profiles=profiles, observations=obs).selected == 'a'
    assert decide(profiles=profiles, observations=obs[:1]).selected == 'a'


@pytest.mark.parametrize('text', ['bad', '[]', '{"item":null}',
    '{"item":{"type":"agent_message","text":"null"}}'])
def test_malformed_output(text):
    result, _ = normalize(text, 0, 'e', 't')
    assert result.status == 'failed'


def test_error_classification_synthetic_codes():
    assert classify_error([], 1) == 'process_error'
    assert classify_error([{'error': {'code': 'usage_limit_reached'}}], 1) == 'exhausted'
    assert classify_error([{'error': 'usage limit?'}], 1) == 'process_error'


def test_artifact_handoff_and_integrity(tmp_path):
    worker = {'summary': 'reversed bounds return upper', 'findings': ['upper wins'],
              'source_refs': ['behavior.py:3'], 'unresolved': ['should raise?']}
    event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(worker)}}
    stream = json.dumps(event) + '\n' + json.dumps({'type': 'turn.completed'})
    result, _ = normalize(stream, 0, 'e', 't')
    assert result.status == 'completed'
    artifact = Artifact.from_result(result)
    path = tmp_path / 'artifact.json'
    path.write_text(artifact.model_dump_json())
    loaded = Artifact.model_validate_json(path.read_text())
    downstream = task(kind='REVIEW', input_refs=(loaded.artifact_id,))
    assert downstream.input_refs == (artifact.artifact_id,)
    assert loaded.result.findings[0].source_refs == ('behavior.py:3',)
    assert 'transcript' not in loaded.model_dump_json()
    altered = loaded.model_dump()
    altered['result']['summary'] = 'tampered'
    with pytest.raises(ValidationError):
        Artifact.model_validate(altered)


def test_structural_not_semantic():
    semantic = SemanticContinuation(execution_id='e', progress=('tests passed',))
    structural = StructuralCheckpoint(execution_id='e', fixture_hash='hash')
    assert semantic.provenance == 'worker_reported'
    assert structural.provenance == 'relay_observed' and structural.observed_usage == {}
    with pytest.raises(ValidationError):
        StructuralCheckpoint(execution_id='e', fixture_hash='x', progress=['tests passed'])


def test_adapter_refuses_real_repository(monkeypatch):
    with pytest.raises(ValueError):
        CodexAdapter().execute(task(), 'snapshot')
    adapter = ClaudeAdapter()
    monkeypatch.setattr(adapter, 'probe', lambda: {'status': 'unavailable', 'reason': 'synthetic auth unavailable'})
    result, events = adapter.execute(task())
    assert result.status == 'blocked' and not events


def test_local_cancel_does_not_claim_remote_stop():
    adapter = CodexAdapter()
    state = adapter.cancel()
    assert state['supported'] is False and state['cancellation_requested'] is False
    assert state['local_process_exited'] is None and state['remote_execution'] == 'unknown'


def test_partial_message_is_not_completion():
    output = {'summary': 'partial', 'findings': [], 'source_refs': [], 'unresolved': []}
    event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(output)}}
    assert normalize(json.dumps(event), 0, 'e', 't')[0].status == 'failed'
