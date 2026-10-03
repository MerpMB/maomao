from datetime import datetime
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Evidence = Literal['official_documentation', 'locally_observed',
                   'unofficial_undocumented', 'design_hypothesis']
Kind = Literal['INVESTIGATE', 'REVIEW']
ExecutionMode = Literal['snapshot_analysis', 'repository_access']
SNAPSHOT_CONTROLS = frozenset({'model_tools_disabled', 'shell_code_disabled',
    'filesystem_tools_disabled', 'browsing_external_disabled', 'inherited_integrations_disabled'})


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal['1'] = '1'


class Control(Contract):
    state: Literal['supported_enforced', 'observed_only', 'unsupported', 'unknown']
    enforcer: str
    evidence: Evidence
    source: str
    interface_support: Literal['documented_supported', 'unknown'] = 'unknown'
    runtime_version: str | None = None
    validation_basis: str = 'No runtime enforcement qualification recorded'


class TaskRequest(Contract):
    task_id: str
    objective: str
    kind: Kind
    execution_mode: ExecutionMode = 'repository_access'
    capabilities: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    input_refs: tuple[str, ...] = ()
    completion_criteria: tuple[str, ...]
    read_scope: tuple[str, ...]
    boundary_requirements: frozenset[str] = frozenset()


class ResourceProfile(Contract):
    resource_id: str
    provider: str
    runtime: str
    execution_modes: frozenset[ExecutionMode] = frozenset({'repository_access'})
    model: str | None = None
    runtime_version: str | None = None
    authentication_mode: str
    capabilities: frozenset[str]
    tools: frozenset[str]
    controls: dict[str, Control] = Field(default_factory=dict)
    pool_id: str
    pool_mapping_source: str
    pool_mapping_evidence: Evidence
    revalidated_at: AwareDatetime


class ResourceObservation(Contract):
    observation_id: str
    pool_id: str
    resource_id: str | None = None
    status: Literal['available', 'exhausted', 'unavailable', 'unknown']
    source: str
    evidence: Evidence
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    reset_at: AwareDatetime | None = None
    failure_classification: str | None = None
    headroom: float | None = Field(default=None, ge=0, le=1)
    accounting_basis: str | None = None

    @model_validator(mode='after')
    def validate_evidence(self):
        if self.expires_at <= self.observed_at:
            raise ValueError('expiry must follow observation')
        if self.headroom is not None and (self.status != 'available' or
                not self.accounting_basis or self.evidence == 'design_hypothesis'):
            raise ValueError('headroom requires available evidence and accounting basis')
        return self


class ReservePolicy(Contract):
    pool_id: str
    preferred_kind: Kind
    release_when_exhausted: str


class RoutingDecision(Contract):
    eligible: tuple[str, ...]
    excluded: dict[str, tuple[str, ...]]
    selected: str | None
    outcome: Literal['selected', 'blocked']
    reserve_effects: tuple[str, ...]
    observation_refs: tuple[str, ...]
    configuration_ref: str
    rationale: str


class Claim(Contract):
    text: str
    source_refs: tuple[str, ...] = ()
    provenance: Literal['worker_reported', 'relay_observed']


class AgentResult(Contract):
    execution_id: str
    task_id: str
    status: Literal['completed', 'failed', 'blocked', 'cancelled']
    summary: str
    findings: tuple[Claim, ...] = ()
    unresolved: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class Artifact(Contract):
    artifact_id: str
    content_hash: str
    result: AgentResult

    @classmethod
    def from_result(cls, result):
        digest = sha256(result.model_dump_json().encode()).hexdigest()
        return cls(artifact_id='sha256:' + digest, content_hash=digest, result=result)

    @model_validator(mode='after')
    def check_hash(self):
        digest = sha256(self.result.model_dump_json().encode()).hexdigest()
        if self.content_hash != digest or self.artifact_id != 'sha256:' + digest:
            raise ValueError('artifact integrity mismatch')
        return self


class StructuralCheckpoint(Contract):
    provenance: Literal['relay_observed'] = 'relay_observed'
    execution_id: str
    process_id: int | None = None
    session_ref: str | None = None
    artifact_refs: tuple[str, ...] = ()
    fixture_hash: str
    observed_usage: dict[str, int] = Field(default_factory=dict)


class SemanticContinuation(Contract):
    provenance: Literal['worker_reported'] = 'worker_reported'
    execution_id: str
    progress: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    remaining_work: tuple[str, ...] = ()
