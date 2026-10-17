import json
from datetime import datetime, timedelta, timezone

from merp_relay import quota
from merp_relay.adapters import normalize_claude

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def rollout(tmp_path, primary, secondary, reached=None, at=NOW):
    day = tmp_path / '2026' / '10' / '18'
    day.mkdir(parents=True)
    limits = {'primary': {'used_percent': primary, 'window_minutes': 300,
                          'resets_at': int((at + timedelta(hours=2)).timestamp())},
              'secondary': {'used_percent': secondary, 'window_minutes': 10080,
                            'resets_at': int((at + timedelta(days=3)).timestamp())},
              'rate_limit_reached_type': reached}
    lines = [{'type': 'event_msg', 'payload': {'type': 'agent_message'}},
             {'timestamp': at.isoformat().replace('+00:00', 'Z'), 'type': 'event_msg',
              'payload': {'type': 'token_count', 'rate_limits': limits}}]
    (day / 'rollout-x.jsonl').write_text('\n'.join(map(json.dumps, lines)), encoding='utf-8')
    return tmp_path


def test_codex_rollout_headroom_uses_binding_window(tmp_path):
    obs, windows = quota.codex_observation(rollout(tmp_path, 8.0, 74.0))
    assert obs.status == 'available' and abs(obs.headroom - 0.26) < 1e-9
    assert obs.accounting_basis == quota.BASIS and obs.evidence == 'locally_observed'
    assert obs.expires_at == NOW + quota.FRESH and [w['name'] for w in windows] == ['300m', '10080m']


def test_codex_exhausted_window_has_no_headroom_and_reset(tmp_path):
    obs, _ = quota.codex_observation(rollout(tmp_path, 100.0, 40.0, reached='primary'))
    assert obs.status == 'exhausted' and obs.headroom is None
    assert obs.reset_at == NOW + timedelta(hours=2)


def test_codex_without_logs_is_unknown(tmp_path):
    assert quota.codex_observation(tmp_path) == (None, [])


def test_claude_cache_latest_per_window(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, 'STATE', tmp_path)
    monkeypatch.setattr(quota, 'CLAUDE_CACHE', tmp_path / 'c.jsonl')
    reset = int((NOW + timedelta(days=1)).timestamp())
    quota.record_claude([{'status': 'allowed', 'rateLimitType': 'seven_day', 'utilization': 0.5, 'resetsAt': reset}],
                        now=NOW - timedelta(minutes=5))
    quota.record_claude([{'status': 'allowed_warning', 'rateLimitType': 'seven_day', 'utilization': 0.93,
                          'resetsAt': reset}], now=NOW)
    obs, windows = quota.claude_observation(tmp_path / 'c.jsonl')
    assert obs.status == 'available' and abs(obs.headroom - 0.07) < 1e-9 and len(windows) == 1


def test_claude_rejected_is_exhausted(tmp_path, monkeypatch):
    monkeypatch.setattr(quota, 'STATE', tmp_path)
    monkeypatch.setattr(quota, 'CLAUDE_CACHE', tmp_path / 'c.jsonl')
    quota.record_claude([{'status': 'rejected', 'rateLimitType': 'five_hour',
                          'resetsAt': int((NOW + timedelta(hours=1)).timestamp())}], now=NOW)
    obs, _ = quota.claude_observation(tmp_path / 'c.jsonl')
    assert obs.status == 'exhausted' and obs.reset_at == NOW + timedelta(hours=1)


def test_claude_stream_json_extracts_rate_limits():
    info = {'status': 'allowed_warning', 'rateLimitType': 'seven_day', 'utilization': 0.93, 'resetsAt': 1}
    result_line = {'type': 'result', 'subtype': 'success', 'is_error': False,
        'structured_output': {'summary': 'ok', 'findings': [], 'source_refs': [], 'unresolved': []}}
    stream = '\n'.join(map(json.dumps, [{'type': 'system'}, {'type': 'rate_limit_event', 'rate_limit_info': info},
                                        result_line]))
    result, metrics = normalize_claude(stream, 0, 'e', 't')
    assert result.status == 'completed' and metrics['rate_limits'] == [info]


def test_claude_typed_rejection_reports_exhausted():
    stream = '\n'.join(map(json.dumps, [{'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
                                        {'type': 'result', 'subtype': 'error', 'is_error': True}]))
    result, _ = normalize_claude(stream, 1, 'e', 't')
    assert result.status == 'failed' and result.summary == 'exhausted'
