from datetime import datetime
from .contracts import ResourceProfile, ResourceObservation, ReservePolicy, RoutingDecision, TaskRequest
from .eligibility import task_profile_eligibility


def route(task: TaskRequest, profiles: list[ResourceProfile],
          observations: list[ResourceObservation], reserves: list[ReservePolicy],
          preference: list[str], now: datetime, configuration_ref: str,
          contained: bool = False) -> RoutingDecision:
    """Expired state becomes unknown; it never releases a reserve or supplies headroom.

    contained: the run is a tool-free packet or a disposable worktree whose diff needs operator
    approval, so unverified runtime controls are waived. Task incompatibilities never are.
    """
    if now.tzinfo is None:
        raise ValueError('now must be timezone aware')
    if len({p.resource_id for p in profiles}) != len(profiles):
        raise ValueError('duplicate resource IDs')
    latest = {}
    for o in sorted(observations, key=lambda o: (o.observed_at, o.observation_id)):
        if o.observed_at <= now:
            latest[o.pool_id] = o
    current = {k: o for k, o in latest.items() if now < o.expires_at}
    excluded, eligible, effects = {}, [], []
    for p in sorted(profiles, key=lambda p: p.resource_id):
        eligibility = task_profile_eligibility(task, p)
        reasons = list(eligibility.incompatibilities if contained else eligibility.reasons)
        o = current.get(p.pool_id)
        if o and o.status in ('exhausted', 'unavailable'):
            reasons.append(f'pool {p.pool_id}: {o.status}')
        for reserve in reserves:
            if reserve.pool_id == p.pool_id and task.kind != reserve.preferred_kind:
                release = current.get(reserve.release_when_exhausted)
                allowed = release is not None and release.status == 'exhausted'
                effects.append(f'{p.resource_id}: reserve {"released" if allowed else "held"}')
                if not allowed:
                    reasons.append(f'reserved for {reserve.preferred_kind}')
        if reasons:
            excluded[p.resource_id] = tuple(reasons)
        else:
            eligible.append(p)
    rank = {r: i for i, r in enumerate(preference)}
    comparable = [current.get(p.pool_id) for p in eligible]
    use_headroom = bool(comparable) and all(o and o.headroom is not None for o in comparable)
    use_headroom = use_headroom and len({o.accounting_basis for o in comparable if o}) == 1
    eligible.sort(key=lambda p: (
        -current[p.pool_id].headroom if use_headroom else 0,
        rank.get(p.resource_id, len(rank)), p.resource_id))
    return RoutingDecision(eligible=tuple(p.resource_id for p in eligible), excluded=excluded,
        selected=eligible[0].resource_id if eligible else None,
        outcome='selected' if eligible else 'blocked', reserve_effects=tuple(effects),
        observation_refs=tuple(sorted(o.observation_id for o in latest.values())),
        configuration_ref=configuration_ref,
        rationale=('Contained run: unverified controls waived. ' if contained else '') +
                  (('Comparable fresh headroom, then preference' if use_headroom else
                    'Configured preference; unknown or expired quota supplies no headroom')
                   if eligible else 'No resource satisfies capability, state and reserve rules'))
