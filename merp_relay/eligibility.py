"""Task/profile compatibility only; pool state, reserves and ranking belong to routing."""
from dataclasses import dataclass
from .contracts import SNAPSHOT_CONTROLS, TaskRequest, ResourceProfile


@dataclass(frozen=True)
class Eligibility:
    incompatibilities: tuple[str, ...]
    control_failures: tuple[str, ...]

    @property
    def reasons(self):
        return self.incompatibilities + self.control_failures


def task_profile_eligibility(task: TaskRequest, profile: ResourceProfile) -> Eligibility:
    incompatible, controls = [], []
    if task.execution_mode not in profile.execution_modes:
        incompatible.append(f'unsupported execution mode: {task.execution_mode}')
    for label, missing in [('capabilities', task.capabilities - profile.capabilities),
                           ('tools', task.tools - profile.tools)]:
        if missing:
            incompatible.append(f'missing {label}: {", ".join(sorted(missing))}')
    requirements = task.boundary_requirements | (SNAPSHOT_CONTROLS
        if task.execution_mode == 'snapshot_analysis' else frozenset())
    for name in sorted(requirements):
        if name not in profile.controls or profile.controls[name].state != 'supported_enforced':
            controls.append(f'boundary not enforced: {name}')
    return Eligibility(tuple(incompatible), tuple(controls))
