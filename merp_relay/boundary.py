from datetime import datetime, timezone
from typing import Literal
from .contracts import Contract, Control, ResourceProfile, SNAPSHOT_CONTROLS
from .eligibility import task_profile_eligibility


class BoundaryEvidence(Contract):
    case_id: str
    outcome: Literal['tool_absent', 'enforced_denial', 'voluntary_refusal',
                     'no_action_observed', 'inconclusive', 'action_succeeded']
    evidence: str
    enforced: bool = False

    def model_post_init(self, context):
        if self.enforced and self.outcome not in ('tool_absent', 'enforced_denial'):
            raise ValueError('outcome cannot prove enforcement')


def runtime_profile(adapter, probe):
    from .adapters import ClaudeAdapter
    claude = isinstance(adapter, ClaudeAdapter)
    version = probe.get('version')
    expected = '2.1.240 (Claude Code)' if claude else 'codex-cli 0.160.0'
    known_version = version == expected
    authenticated = probe.get('status') == 'available' and probe.get('subscription_authenticated') is True
    basis = ('No combined-path enforcement validation; subscription probe verified' if authenticated
             else 'No combined-path enforcement validation; subscription probe missing or unavailable')
    if not known_version:
        basis = 'Runtime version not validated; no enforcement qualification'
    controls = {}
    for name in sorted(SNAPSHOT_CONTROLS):
        # Interface support is not qualification. No real validation record exists for either path.
        documented = known_version and (claude or name == 'shell_code_disabled')
        controls[name] = Control(state='unknown',
            enforcer='runtime configuration (unvalidated)', evidence='official_documentation' if documented else 'design_hypothesis',
            interface_support='documented_supported' if documented else 'unknown',
            runtime_version=version, validation_basis=basis,
            source='https://code.claude.com/docs/en/cli-reference' if claude else
                   'https://learn.chatgpt.com/docs/config-file/config-reference')
    for name in ('filesystem_read_scope', 'filesystem_write_os', 'runtime_transport_network',
                 'local_process_tree_cancellation', 'remote_execution_cancellation'):
        controls[name] = Control(state='unknown', enforcer='unknown', evidence='design_hypothesis',
                                 runtime_version=version, validation_basis='No local enforcement validation',
                                 source='not established by snapshot execution')
    return ResourceProfile(resource_id='claude' if claude else 'codex', provider='anthropic' if claude else 'openai',
        runtime='claude' if claude else 'codex', runtime_version=probe.get('version'),
        execution_modes=frozenset({'snapshot_analysis'}), authentication_mode='subscription',
        capabilities=frozenset({'snapshot_reasoning'}), tools=frozenset(), controls=controls,
        pool_id='claude-local' if claude else 'codex-local', pool_mapping_source='unverified local mapping',
        pool_mapping_evidence='design_hypothesis', revalidated_at=datetime.now(timezone.utc))


def admission_reasons(task, profile):
    return list(task_profile_eligibility(task, profile).reasons)
