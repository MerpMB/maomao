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


def test_claude_result_envelope_need_not_be_last_line():
    # Observed with 2.1.240: a system task_summary line follows the result envelope.
    result_line = {'type': 'result', 'subtype': 'success', 'is_error': False,
        'structured_output': {'summary': 'ok', 'findings': [], 'source_refs': [], 'unresolved': []}}
    stream = '\n'.join(map(json.dumps, [result_line, {'type': 'system', 'subtype': 'task_summary', 'detail': None}]))
    result, _ = normalize_claude(stream, 0, 'e', 't')
    assert result.status == 'completed' and result.summary == 'ok'


def test_claude_typed_rejection_reports_exhausted():
    stream = '\n'.join(map(json.dumps, [{'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
                                        {'type': 'result', 'subtype': 'error', 'is_error': True}]))
    result, _ = normalize_claude(stream, 1, 'e', 't')
    assert result.status == 'failed' and result.summary == 'exhausted'


def test_claude_window_that_reset_since_reading_is_unknown(tmp_path, monkeypatch):
    # Observed live: a pre-reset 94% seven_day reading kept supplying 6% headroom after the reset.
    monkeypatch.setattr(quota, 'STATE', tmp_path)
    monkeypatch.setattr(quota, 'CLAUDE_CACHE', tmp_path / 'c.jsonl')
    quota.record_claude([{'status': 'allowed_warning', 'rateLimitType': 'seven_day', 'utilization': 0.94,
                          'resetsAt': int((NOW - timedelta(hours=1)).timestamp())}], now=NOW - timedelta(hours=3))
    quota.record_claude([{'status': 'allowed', 'rateLimitType': 'five_hour',
                          'resetsAt': int((NOW + timedelta(hours=4)).timestamp())}], now=NOW)
    obs, windows = quota.claude_observation(tmp_path / 'c.jsonl')
    assert obs.status == 'available' and obs.headroom is None
    assert {w['name']: w['used'] for w in windows} == {'five_hour': None, 'seven_day': None}


def test_claude_unified_windows_give_both_limits(tmp_path, monkeypatch):
    # Claude Code 2.1.302 reports every window in one event.
    monkeypatch.setattr(quota, 'STATE', tmp_path)
    monkeypatch.setattr(quota, 'CLAUDE_CACHE', tmp_path / 'c.jsonl')
    five, seven = int((NOW + timedelta(hours=2)).timestamp()), int((NOW + timedelta(days=5)).timestamp())
    quota.record_claude([{'status': 'allowed', 'rateLimitType': 'five_hour', 'resetsAt': five,
                          'unifiedWindows': {'five_hour': {'utilization': 0.43, 'resetsAt': five},
                                             'seven_day': {'utilization': 0.06, 'resetsAt': seven}}}], now=NOW)
    obs, windows = quota.claude_observation(tmp_path / 'c.jsonl')
    assert {w['name']: w['used'] for w in windows} == {'five_hour': 0.43, 'seven_day': 0.06}
    assert abs(obs.headroom - 0.57) < 1e-9


def test_codex_newest_reading_wins_over_newest_file(tmp_path):
    import os
    old_day, new_day = tmp_path / '2026' / '10' / '16', tmp_path / '2026' / '10' / '18'
    for day, used, at in ((old_day, 8.0, NOW - timedelta(hours=5)), (new_day, 82.0, NOW)):
        day.mkdir(parents=True)
        event = {'timestamp': at.isoformat().replace('+00:00', 'Z'), 'type': 'event_msg',
                 'payload': {'type': 'token_count', 'rate_limits': {'primary': {
                     'used_percent': used, 'window_minutes': 300,
                     'resets_at': int((NOW + timedelta(hours=1)).timestamp())}}}}
        (day / 'rollout-x.jsonl').write_text(json.dumps(event) + '\n', encoding='utf-8')
    # The old thread's file was touched last (a long-lived desktop session), but its reading is older.
    later = (NOW + timedelta(minutes=1)).timestamp()
    os.utime(old_day / 'rollout-x.jsonl', (later, later))
    obs, windows = quota.codex_observation(tmp_path)
    assert windows[0]['used'] == 0.82 and obs.observed_at == NOW


def test_refresh_records_claude_and_is_single_flight(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(quota, 'STATE', tmp_path)
    monkeypatch.setattr(quota, 'CLAUDE_CACHE', tmp_path / 'c.jsonl')
    monkeypatch.setattr('shutil.which', lambda name: 'claude-cli')
    monkeypatch.setattr('merp_relay.adapters.codex_executable', lambda: 'codex-cli')
    info = {'status': 'allowed', 'rateLimitType': 'five_hour', 'utilization': 0.5,
            'resetsAt': int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}
    seen = []
    def fake_run(command, **kwargs):
        seen.append(command)
        assert '--model' not in command or command[command.index('--model') + 1] == 'haiku'
        out = json.dumps({'type': 'rate_limit_event', 'rate_limit_info': info}) if command[0] == 'claude-cli' else ''
        return SimpleNamespace(returncode=0, stdout=out.encode(), stderr=b'')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert quota.refresh() == {'claude': 'refreshed', 'codex': 'refreshed'}
    assert [c[0] for c in seen] == ['claude-cli', 'codex-cli']
    assert quota.claude_observation(tmp_path / 'c.jsonl')[1][0]['used'] == 0.5
    quota._refreshing.acquire()
    try:
        assert quota.refresh() is None  # a second click while one runs does nothing
    finally:
        quota._refreshing.release()
