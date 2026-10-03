from datetime import datetime, timezone
from typing import Literal
from .contracts import Contract, Control, ResourceProfile, SNAPSHOT_CONTROLS


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
    controls = {}
    for name in SNAPSHOT_CONTROLS:
        # Codex has no verified complete built-in tool allowlist in M0.1.
        enforced = claude and name != 'inherited_integrations_disabled'
        controls[name] = Control(state='supported_enforced' if enforced else 'unknown',
            enforcer='runtime configuration', evidence='official_documentation',
            source='Claude --tools empty / --safe-mode; managed hooks remain unknown' if claude else
                   'Codex shell/web config verified; complete tool inventory not verified')
    for name in ('filesystem_read_scope', 'filesystem_write_os', 'runtime_transport_network',
                 'local_process_tree_cancellation', 'remote_execution_cancellation'):
        controls[name] = Control(state='unknown', enforcer='unknown', evidence='design_hypothesis',
                                 source='not established by snapshot execution')
    return ResourceProfile(resource_id='claude' if claude else 'codex', provider='anthropic' if claude else 'openai',
        runtime='claude' if claude else 'codex', runtime_version=probe.get('version'),
        execution_modes=frozenset({'snapshot_analysis'}), authentication_mode='subscription',
        capabilities=frozenset({'snapshot_reasoning'}), tools=frozenset(), controls=controls,
        pool_id='claude-local' if claude else 'codex-local', pool_mapping_source='unverified local mapping',
        pool_mapping_evidence='design_hypothesis', revalidated_at=datetime.now(timezone.utc))


def admission_reasons(task, profile):
    reasons = []
    if task.execution_mode not in profile.execution_modes:
        reasons.append('unsupported execution mode')
    for name in sorted(SNAPSHOT_CONTROLS | task.boundary_requirements):
        if name not in profile.controls or profile.controls[name].state != 'supported_enforced':
            reasons.append(f'control not enforced: {name}')
    return reasons
