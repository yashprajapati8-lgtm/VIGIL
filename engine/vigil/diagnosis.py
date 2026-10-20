"""Diagnose: find the service that actually stopped, not the one that screams loudest.

The blame walk is like walking along a traffic jam: you do not blame the car at
the back, you drive forward until you reach the car that stopped. VIGIL starts at
the anomalous service closest to users and follows calls that are failing into
other anomalous services, until it reaches one whose own dependencies are fine.

The walk is deterministic and explainable. The LLM (llm.py) diagnoses the same
evidence independently, without seeing the walk's answer, and VIGIL only trusts
the result fully when both agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .collector import EdgeSample, ServiceSample, Snapshot
from .detector import LABELS, SYMPTOMS, Verdict

CATEGORIES = [
    "crashed",
    "resource_leak",
    "stuck_process",
    "load_saturation",
    "regression_after_change",
    "unknown",
]

CATEGORY_LABEL = {
    "crashed": "crashed",
    "resource_leak": "memory leak",
    "stuck_process": "stuck process",
    "load_saturation": "load saturation",
    "regression_after_change": "regression after a deploy",
    "unknown": "unknown cause",
}

CATEGORY_TEXT = {
    "crashed": "the process is down",
    "resource_leak": "memory is leaking",
    "stuck_process": "requests stall while traffic is flat (stuck workers or a runaway loop)",
    "load_saturation": "real demand outgrew capacity",
    "regression_after_change": "it broke right after a version change",
    "unknown": "no known pattern fits",
}


@dataclass
class WalkResult:
    root: str
    path: list[str]
    steps: list[str]
    own_signals: list[str]
    conclusive: bool
    symptoms: dict[str, str] = field(default_factory=dict)
    ruled_out: dict[str, str] = field(default_factory=dict)
    cleared: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


ONSET_BUCKET_S = 10  # a slow service reports late: starts this close together count as simultaneous


def errors_explained(caller: ServiceSample | None, e: EdgeSample) -> float | None:
    """Share of the caller's own errors that are failed calls to this target (0..1)."""
    if caller is None or not caller.rps or caller.error_rate is None or e.fail_ratio is None or not e.rps:
        return None
    caller_errors = caller.error_rate * caller.rps
    if caller_errors < 0.1:
        return None
    return min(1.0, (e.fail_ratio * e.rps) / caller_errors)


def edge_is_bad(e: EdgeSample, fail_threshold: float, p95_threshold_ms: float) -> bool:
    return (e.fail_ratio or 0.0) >= fail_threshold or (e.p95_ms or 0.0) >= p95_threshold_ms


def callee_confirms(v: Verdict | None, z_flag: float = 3.0) -> bool:
    """Does the called service itself look slow or failing? Edge metrics are measured
    by the caller: a caller starved for CPU waits late for replies and records them
    as slow or timed out even when the callee answered fast. Only blame the callee
    when its own server-side latency or errors agree."""
    if v is None or v.state == "down":
        return True
    z = v.z or {}
    return z.get("p95_ms", 0.0) > z_flag or z.get("error_rate", 0.0) > z_flag


def _edge_text(e: EdgeSample) -> str:
    parts = []
    if e.fail_ratio is not None:
        parts.append(f"{e.fail_ratio:.0%} of calls failing")
    if e.p95_ms is not None:
        parts.append(f"p95 {e.p95_ms:.0f} ms")
    return ", ".join(parts)


def blame_walk(services: list[str], verdicts: dict[str, Verdict], snap: Snapshot,
               known_edges: set[tuple[str, str]], fail_threshold: float = 0.05,
               p95_threshold_ms: float = 500) -> WalkResult:
    in_incident = set(services)
    edges = {(e.caller, e.target): e for e in snap.edges}

    def callers(s: str) -> set[str]:
        return {a for a, b in known_edges if b == s}

    # Start at the incident service closest to users: no other incident service calls it.
    starts = [s for s in services if not (callers(s) & in_incident)]
    node = starts[0] if starts else services[0]
    path, steps = [node], []
    caller_side: list[str] = []      # bad edges whose callee answered normally
    while True:
        nxt = None
        for (a, b), e in sorted(edges.items()):
            if a == node and b in in_incident and b not in path and edge_is_bad(e, fail_threshold, p95_threshold_ms):
                if not callee_confirms(verdicts.get(b)):
                    if b not in caller_side:
                        caller_side.append(b)
                        steps.append(f"{a} -> {b} looks slow ({_edge_text(e)}), but {b} answers its own requests "
                                     f"normally: the wait is inside {a}")
                    continue
                nxt = b
                steps.append(f"{a} -> {b}: {_edge_text(e)}")
                break
        if nxt is None:
            break
        path.append(nxt)
        node = nxt

    root = node
    v = verdicts.get(root)
    own = list(v.reasons) if v else []
    conclusive = bool(own) or (v is not None and v.state == "down")
    bad_out = [e for (a, b), e in edges.items()
               if a == root and edge_is_bad(e, fail_threshold, p95_threshold_ms)]
    # Incident services the root calls successfully: anomalous too (say, from the same
    # surge of traffic), but the root's calls to them work, so they did not cause it.
    cleared = sorted({b for (a, b), e in edges.items()
                      if a == root and b in in_incident and not edge_is_bad(e, fail_threshold, p95_threshold_ms)}
                     | {b for b in caller_side if (root, b) in edges})
    if bad_out:
        steps.append(f"{root}'s own calls to {', '.join(e.target for e in bad_out)} also fail, "
                     "but those services are not monitored")
    else:
        steps.append(f"its own dependencies are healthy")

    result = WalkResult(root=root, path=path, steps=steps, own_signals=own, conclusive=conclusive, cleared=cleared)
    for s in services:
        if s == root:
            continue
        out = edges.get((s, path[path.index(s) + 1])) if s in path[:-1] else None
        sv = snap.services.get(s)
        healthy_bits = []
        if sv and verdicts.get(s):
            z = verdicts[s].z
            if z.get("cpu_pct", 0) < 3:
                healthy_bits.append("CPU normal")
            if z.get("mem_mb", 0) < 3 and z.get("mem_slope_mb_s", 0) < 3:
                healthy_bits.append("memory normal")
        share = errors_explained(sv, out) if out else None
        if out and share is not None:
            why = f"{share:.0%} of its errors are failed calls to {out.target} ({_edge_text(out)})"
        elif out:
            why = f"its failures are calls to {out.target} ({_edge_text(out)})"
        elif s in caller_side and (root, s) in edges:
            why = (f"{root}'s calls to it look slow ({_edge_text(edges[(root, s)])}), but it answers its own "
                   f"requests normally: {root} is too busy to collect the replies")
        elif s in cleared and (root, s) in edges:
            why = f"busy too, but {root}'s calls to it succeed ({_edge_text(edges[(root, s)])}): not the cause"
        else:
            why = "anomalous, but downstream of nothing that explains it"
        result.symptoms[s] = why
        if out:
            result.ruled_out[s] = f"{', '.join(healthy_bits) or 'own resources fine'}; {why}"
    return result


def classify(root: str, verdict: Verdict | None, recent_changes: list[dict], now: float,
             change_window_s: float = 600) -> tuple[str, str]:
    """Deterministic category for the root. Policy acts on this, never on LLM prose."""
    if verdict is None:
        return "unknown", "no verdict for the root"
    if verdict.state == "down":
        return "crashed", "the process is down"
    change = next((c for c in reversed(recent_changes)
                   if c["service"] == root and now - c["ts"] <= change_window_s), None)
    if change:
        mins = max(1, round((now - change["ts"]) / 60))
        return "regression_after_change", f"started {mins} min after {root} changed {change['from']} -> {change['to']}"
    z = verdict.z
    if verdict.time_to_oom_s or z.get("mem_slope_mb_s", 0) > 4:
        tte = f", memory limit in ~{verdict.time_to_oom_s}s" if verdict.time_to_oom_s else ""
        growth = verdict.texts.get("mem_slope_mb_s", "memory growing")
        return "resource_leak", f"{growth}{tte}"
    if z.get("rps", 0) > 3 and z.get("cpu_per_req", 0) < 2:
        return "load_saturation", f"{verdict.texts.get('rps', 'traffic up')} while CPU per request stays normal"
    if z.get("p95_ms", 0) > 3 or z.get("cpu_per_req", 0) > 3 or z.get("error_rate", 0) > 3:
        lead = max(("p95_ms", "cpu_per_req", "error_rate"), key=lambda k: z.get(k, 0))
        return "stuck_process", f"{verdict.texts.get(lead, LABELS[lead] + ' up')} while traffic is flat"
    return "unknown", "no rule matched"


def build_evidence(incident_id: str, services: list[str], verdicts: dict[str, Verdict], snap: Snapshot,
                   medians: dict[str, dict[str, float]], onsets: dict[str, float],
                   recent_changes: list[dict]) -> dict:
    """Evidence with stable IDs. Deliberately excludes the walk's verdict."""
    t0 = min(onsets.values()) if onsets else snap.ts
    items: list[dict] = []

    def onset(svc: str) -> float | None:
        if svc not in onsets:
            return None
        return float(int((onsets[svc] - t0) // ONSET_BUCKET_S) * ONSET_BUCKET_S)

    def add(**kv):
        items.append({"id": f"E{len(items) + 1}", **kv})

    for s in services:
        sample: ServiceSample | None = snap.services.get(s)
        v = verdicts.get(s)
        if sample is None or v is None:
            continue
        if v.state == "down":
            add(service=s, signal="up", value=0, note="process down")
            continue
        add(service=s, signal="up", value=1, note="process running and answering health checks")
        for sig in list(SYMPTOMS) + ["rps", "cpu_per_req"]:
            value = getattr(sample, sig)
            if value is None:
                continue
            add(service=s, signal=sig, value=round(value, 4),
                baseline=round(medians.get(s, {}).get(sig, float("nan")), 4) if sig in medians.get(s, {}) else None,
                z=v.z.get(sig), onset_s=onset(s))
    for e in snap.edges:
        if e.caller in services or e.target in services:
            if e.fail_ratio is not None:
                add(edge=f"{e.caller}->{e.target}", signal="failed_call_ratio", value=round(e.fail_ratio, 4))
            if e.p95_ms is not None:
                add(edge=f"{e.caller}->{e.target}", signal="call_p95_ms", value=round(e.p95_ms, 1))
            share = errors_explained(snap.services.get(e.caller), e)
            if share is not None:
                add(service=e.caller, signal=f"share_of_errors_that_are_failed_calls_to_{e.target}",
                    value=round(share, 3))
    calls = sorted({(e.caller, e.target) for e in snap.edges if e.caller in services or e.target in services})
    return {
        "incident": incident_id,
        "services": services,
        "calls": [list(c) for c in calls],
        "evidence": items,
        "recent_changes": [{k: c[k] for k in ("service", "from", "to")} | {"minutes_ago": round((snap.ts - c["ts"]) / 60, 1)}
                           for c in recent_changes],
        "categories": CATEGORIES,
    }


def evidence_types(walk: WalkResult, onsets: dict[str, float], tick_s: float, multi_service: bool) -> list[str]:
    types = []
    if walk.conclusive:
        types.append("metrics")
    if multi_service and (len(walk.path) > 1 or walk.cleared):
        types.append("edges")     # failing calls lead to the root, or the root's own calls are proven fine
    if walk.root in onsets and onsets[walk.root] <= min(onsets.values()) + ONSET_BUCKET_S:
        types.append("timing")
    return types


def confidence(walk: WalkResult, rule_category: str, llm: dict, types: list[str],
               multi_service: bool) -> tuple[str, list[str]]:
    """Confidence is agreement between independent evidence, never a model's self-score."""
    needed = 3 if multi_service else 2
    reasons = [f"evidence types: {', '.join(types) or 'none'}"]
    if not walk.conclusive:
        return "Low", reasons + [f"{walk.root} shows no signal of its own"]
    if llm.get("status") != "ok":
        return "Medium", reasons + [f"LLM {llm.get('status')}: walk alone, capped at Medium"]
    if llm["root_cause_service"] != walk.root:
        return "Low", reasons + [f"walk says {walk.root}, LLM says {llm['root_cause_service']}"]
    reasons.append(f"walk and LLM independently agree on {walk.root}")
    if llm["category"] != rule_category:
        return "Medium", reasons + [f"category differs (rules: {rule_category}, LLM: {llm['category']})"]
    if len(types) < needed:
        return "Medium", reasons + [f"only {len(types)} of {needed} evidence types"]
    return "High", reasons
