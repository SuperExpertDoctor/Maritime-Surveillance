"""Pure observation-based motion features and progressive probe evidence.

Times are minutes, positions/ranges are cells, and physical speeds are knots.
Only the simulation thread advances a frozen session. A baseline phase with no
baseline start represents transit to the baseline observation band.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import math
from statistics import fmean, median

from src.mission.config import ContactConfig
from src.mission.contracts import (
    ContactSnapshot, ObservationSample, ProbeSession, TrajectoryFeatures,
)


__all__ = ("build_features", "advance_probe", "select_keypoints")


DISTANCE_TOLERANCE_CELLS = .15
# select_keypoints has no config argument; use the design's default continuity.
DEFAULT_MAX_SAMPLE_GAP_MIN = 2.


def wrap_delta_deg(current: float, previous: float) -> float:
    return (current - previous + 180.) % 360. - 180.


def _key(sample: ObservationSample) -> tuple[float, str]:
    return sample.observed_at_min, sample.sample_id


def _finite_pair(value) -> bool:
    return value is not None and len(value) == 2 and all(math.isfinite(v) for v in value)


def _valid(sample: ObservationSample) -> bool:
    return (math.isfinite(sample.observed_at_min) and _finite_pair(sample.position_cells)
            and sample.source in ("ais", "eo", "sar") and bool(sample.source_id))


def _ordered(samples) -> tuple[ObservationSample, ...]:
    """Stable deduplication: one independent fix per source and timestamp."""
    seen_ids, seen_fixes = set(), set()
    result = []
    for sample in sorted((s for s in samples if _valid(s)), key=_key):
        fix = (sample.source, sample.source_id, sample.observed_at_min)
        if sample.sample_id not in seen_ids and fix not in seen_fixes:
            result.append(sample)
            seen_ids.add(sample.sample_id)
            seen_fixes.add(fix)
    return tuple(result)


def _streams(samples):
    streams = defaultdict(list)
    for sample in _ordered(samples):
        streams[sample.source, sample.source_id].append(sample)
    return tuple(streams.values())


def _visual_range(sample: ObservationSample) -> float | None:
    distance = sample.measured_range_cells
    if (sample.source in ("eo", "sar") and _finite_pair(sample.observer_position_cells)
            and distance is not None and math.isfinite(distance) and distance >= 0.):
        return distance
    return None


def _in_phase(sample: ObservationSample, probe: ProbeSession,
              phase: str, config: ContactConfig) -> bool:
    distance = _visual_range(sample)
    if distance is None or sample.source_id != probe.uav_id:
        return False
    if phase == "baseline":
        return abs(distance - config.baseline_standoff_cells) <= DISTANCE_TOLERANCE_CELLS + 1e-12
    return distance <= config.near_standoff_cells + DISTANCE_TOLERANCE_CELLS + 1e-12


def _duration(samples, max_gap: float) -> float:
    """Union of adjacent same-stream intervals; simultaneous sensors do not add time."""
    intervals = sorted((a.observed_at_min, b.observed_at_min)
                       for stream in _streams(samples) for a, b in zip(stream, stream[1:])
                       if 0. < b.observed_at_min - a.observed_at_min <= max_gap)
    total, end = 0., -math.inf
    for start, stop in intervals:
        total += max(0., stop - max(start, end))
        end = max(end, stop)
    return total


def _median3(values):
    # Endpoints lack a complete window and remain unchanged. No smoothing
    # crosses a source, a missing measurement, or an observation gap.
    return [median(values[i - 1:i + 2]) if 0 < i < len(values) - 1 else v
            for i, v in enumerate(values)]


def _motion(samples, max_gap: float):
    """Return smoothed motion metrics and the sample ending each turn."""
    speeds, rates, arcs, turn_samples = [], [], [], []
    for stream in _streams(samples):
        segments = []
        for sample in stream:
            if not segments or sample.observed_at_min - segments[-1][-1].observed_at_min > max_gap:
                segments.append([])
            segments[-1].append(sample)
        for segment in segments:
            velocities = []
            for i, sample in enumerate(segment):
                velocity = sample.velocity_cells_min
                if not _finite_pair(velocity):
                    velocity = None
                    if i:
                        previous = segment[i - 1]
                        dt = sample.observed_at_min - previous.observed_at_min
                        velocity = tuple((p - q) / dt for p, q in zip(
                            sample.position_cells, previous.position_cells))
                velocities.append(velocity)
            speeds.extend(_median3([math.hypot(*v) for v in velocities if v is not None]))
            heading_runs = []
            for sample, velocity in zip(segment, velocities):
                if velocity is None or math.hypot(*velocity) == 0.:
                    heading_runs.append([])
                    continue
                heading = math.degrees(math.atan2(velocity[1], velocity[0]))
                if not heading_runs:
                    heading_runs.append([])
                run = heading_runs[-1]
                if run:
                    heading = run[-1][1] + wrap_delta_deg(heading, run[-1][1])
                run.append((sample, heading))
            for run in heading_runs:
                if len(run) < 2:
                    continue
                smoothed = _median3([heading for _, heading in run])
                for i, (a, b) in enumerate(zip(smoothed, smoothed[1:])):
                    rate = abs(b - a) / (
                        run[i + 1][0].observed_at_min - run[i][0].observed_at_min
                    )
                    rates.append(rate)
                    turn_samples.append((rate, run[i + 1][0]))
                arcs.append((
                    run[-1][0].observed_at_min - run[0][0].observed_at_min,
                    smoothed[-1] - smoothed[0],
                ))
    return speeds, rates, arcs, turn_samples


def _reset_phase(probe: ProbeSession) -> ProbeSession:
    changes = dict(_phase_samples=(), close_exposure_min=0.)
    if probe.phase == "baseline":
        changes.update(baseline_sample_ids=(), near_sample_ids=())
    elif probe.phase in ("near", "closing"):
        changes.update(near_sample_ids=())
    return replace(probe, **changes)


def _timeout(probe: ProbeSession, now: float, config: ContactConfig) -> ProbeSession:
    if probe.phase == "finished":
        return probe
    if probe.baseline_started_at_min is None:
        expired = now - probe.started_at_min >= config.approach_timeout_min
        reason = "approach_timeout"
    else:
        expired = now - probe.baseline_started_at_min >= config.probe_timeout_min
        reason = "probe_timeout"
    return replace(probe, phase="finished", completed_reason=reason) if expired else probe


def advance_probe(probe: ProbeSession, new_samples: tuple[ObservationSample, ...],
                  now_min: float, config: ContactConfig) -> ProbeSession:
    """Advance from measurements, never from wall-clock exposure or vessel truth.

    Input may be incremental or a replayed snapshot. Late fixes cannot change
    an already executed phase. Leaving the near band returns to closing and
    discards that near attempt; the first baseline deadline remains fixed.
    """
    if not math.isfinite(now_min):
        raise ValueError("now_min must be finite")
    if probe.phase == "finished":
        return probe
    marker = (probe.phase, probe.phase_started_at_min)
    if probe._phase_marker is not None and probe._phase_marker != marker:
        probe = _reset_phase(probe)
    for sample in _ordered(new_samples):
        # Only the assigned UAV's visual evidence can advance the watermark.
        if sample.source not in ("eo", "sar") or sample.source_id != probe.uav_id:
            continue
        if (sample.contact_id != probe.contact_id or sample.observed_at_min > now_min
                or sample.observed_at_min < max(probe.started_at_min, probe.phase_started_at_min)
                or (probe._last_sample_key is not None and _key(sample) <= probe._last_sample_key)):
            continue
        # A later delivery of a retained fix is not new range evidence and
        # must not change the watermark, phase, or accumulated exposure.
        if any(sample.sample_id == retained.sample_id or (
                sample.source == retained.source and sample.source_id == retained.source_id
                and sample.observed_at_min == retained.observed_at_min)
               for retained in probe._phase_samples):
            continue
        probe = _timeout(probe, sample.observed_at_min, config)
        if probe.phase == "finished":
            return probe
        probe = replace(probe, _last_sample_key=_key(sample))
        if probe.phase == "awaiting_assessment":
            continue
        if (probe._phase_samples and sample.observed_at_min -
                probe._phase_samples[-1].observed_at_min > config.max_sample_gap_min):
            probe = _reset_phase(probe)
        if probe.phase == "closing":
            if not _in_phase(sample, probe, "near", config):
                continue
            probe = replace(_reset_phase(probe), phase="near",
                            phase_started_at_min=sample.observed_at_min)
        if not _in_phase(sample, probe, probe.phase, config):
            probe = _reset_phase(probe)
            if probe.phase == "near":
                probe = replace(probe, phase="closing", phase_started_at_min=sample.observed_at_min)
            continue
        if probe.phase == "baseline" and probe.baseline_started_at_min is None:
            probe = replace(probe, baseline_started_at_min=sample.observed_at_min,
                            phase_started_at_min=sample.observed_at_min)
        samples = _ordered((*probe._phase_samples, sample))
        ids = tuple(s.sample_id for s in samples)
        duration = _duration(samples, config.max_sample_gap_min)
        phase = probe.phase
        probe = replace(probe, _phase_samples=samples, **{f"{phase}_sample_ids": ids})
        if phase == "near":
            probe = replace(probe, close_exposure_min=duration)
        threshold = config.baseline_duration_min if phase == "baseline" else config.near_duration_min
        if duration >= threshold and len(samples) >= config.min_valid_samples_per_phase:
            probe = replace(probe, phase="closing" if phase == "baseline" else "awaiting_assessment",
                            phase_started_at_min=sample.observed_at_min, _phase_samples=())
    probe = _timeout(probe, now_min, config)
    if (probe.phase in ("baseline", "near") and probe._phase_samples
            and now_min - probe._phase_samples[-1].observed_at_min > config.max_sample_gap_min):
        probe = _reset_phase(probe)
    return replace(probe, _phase_marker=(probe.phase, probe.phase_started_at_min))


def build_features(contact: ContactSnapshot, probe: ProbeSession, now_min: float,
                   config: ContactConfig) -> TrajectoryFeatures:
    """Resolve evidence against this snapshot revision and summarize observable history."""
    if contact.contact_id != probe.contact_id:
        raise ValueError("contact and probe must refer to the same contact")
    if not math.isfinite(now_min):
        raise ValueError("now_min must be finite")
    samples = _ordered(s for s in contact.samples
                       if s.contact_id == contact.contact_id and s.observed_at_min <= now_min)
    by_id = {s.sample_id: s for s in samples}
    baseline = tuple(s for s in samples if s.sample_id in probe.baseline_sample_ids)
    near = tuple(s for s in samples if s.sample_id in probe.near_sample_ids)
    # Phase IDs define membership, but AIS or missing observer context cannot
    # supply duration/count thresholds, even when supplied by an external caller.
    baseline_valid = tuple(s for s in baseline if _in_phase(s, probe, "baseline", config))
    near_valid = tuple(s for s in near if _in_phase(s, probe, "near", config))
    baseline_duration = _duration(baseline_valid, config.max_sample_gap_min)
    near_duration = _duration(near_valid, config.max_sample_gap_min)
    baseline_speeds, baseline_turns, _, _ = _motion(
        baseline, config.max_sample_gap_min
    )
    near_speeds, near_turns, _, _ = _motion(near, config.max_sample_gap_min)
    # Use this UAV's visual track when available, otherwise auxiliary history.
    own = tuple(s for s in samples if s.source != "ais" and s.source_id == probe.uav_id)
    _, _, arcs, _ = _motion(own or samples, config.max_sample_gap_min)
    ranges = [distance for s in samples if (distance := _visual_range(s)) is not None]
    gaps = [b.observed_at_min - a.observed_at_min for stream in _streams(samples)
            for a, b in zip(stream, stream[1:])]
    max_gap = max(gaps, default=0.)
    land_fraction = sum(s.navigation_context == "near_land" for s in samples) / len(samples) if samples else 0.
    confounders = []
    if land_fraction:
        confounders.append("near_land")
    if max_gap > config.max_sample_gap_min:
        confounders.append("observation_gap")
    if any(sid not in by_id for sid in (*probe.baseline_sample_ids, *probe.near_sample_ids)):
        confounders.append("missing_evidence")
    # Include immediately preceding approach context, but allow a clean
    # baseline after withdrawal or a reset of the retained baseline evidence.
    baseline_start = min((s.observed_at_min for s in baseline), default=probe.phase_started_at_min)
    baseline_end = max((s.observed_at_min for s in baseline), default=probe.phase_started_at_min)
    baseline_confounded = any(
        baseline_start - config.max_sample_gap_min <= s.observed_at_min <= baseline_end
        and (distance := _visual_range(s)) is not None
        and distance <= config.near_standoff_cells + DISTANCE_TOLERANCE_CELLS
        for s in samples)
    if baseline_confounded:
        confounders.append("baseline_confounded")
    context = bool(baseline_valid and near_valid and
                   max(s.observed_at_min for s in baseline_valid) < min(s.observed_at_min for s in near_valid))
    sufficient = (baseline_duration >= config.baseline_duration_min
                  and near_duration >= config.near_duration_min
                  and len(baseline_valid) >= config.min_valid_samples_per_phase
                  and len(near_valid) >= config.min_valid_samples_per_phase
                  and context and not baseline_confounded and probe.baseline_started_at_min is not None
                  and now_min - probe.baseline_started_at_min < config.probe_timeout_min
                  and probe.phase != "finished" and "missing_evidence" not in confounders)
    scale = config.cell_size_km
    if scale is not None and (not math.isfinite(scale) or scale <= 0.):
        raise ValueError("cell_size_km must be positive and finite")

    def knots(values):
        return fmean(values) * scale * 60. / 1.852 if values and scale is not None else None

    return TrajectoryFeatures(
        contact_id=contact.contact_id, history_revision=contact.revision, probe_id=probe.probe_id,
        baseline_sample_ids=tuple(s.sample_id for s in baseline),
        near_sample_ids=tuple(s.sample_id for s in near),
        baseline_duration_min=baseline_duration, near_duration_min=near_duration,
        baseline_speed_mean_kn=knots(baseline_speeds), near_speed_mean_kn=knots(near_speeds),
        baseline_abs_turn_rate_deg_min=fmean(baseline_turns) if baseline_turns else None,
        near_abs_turn_rate_deg_min=fmean(near_turns) if near_turns else None,
        heading_change_deg=max(arcs, key=lambda arc: arc[0])[1] if arcs else None,
        min_observed_uav_distance_cells=min(ranges) if ranges else None,
        close_exposure_min=near_duration, near_land_fraction=land_fraction,
        max_observation_gap_min=max_gap, sufficient_evidence=sufficient,
        confounders=tuple(confounders))


def select_keypoints(samples, limit: int) -> tuple[ObservationSample, ...]:
    """Keep endpoints, approach onset, closest fix and largest turn, then fill.

    Approach onset is the fix before the first measured range decrease in a
    visual stream. Same-stream event neighbors precede stable uniform fill.
    With a smaller budget, endpoints and event centers take priority.
    """
    samples = _ordered(samples)
    limit = min(max(0, limit), 12, len(samples))
    if not limit:
        return ()
    index = {s.sample_id: i for i, s in enumerate(samples)}
    approaches = []
    neighbors = {}
    for stream in _streams(samples):
        for i, sample in enumerate(stream):
            neighbors[index[sample.sample_id]] = tuple(
                index[stream[j].sample_id] for j in (i - 1, i + 1) if 0 <= j < len(stream))
        for a, b in zip(stream, stream[1:]):
            da, db = _visual_range(a), _visual_range(b)
            if da is not None and db is not None and db < da:
                approaches.append(index[a.sample_id])
    _, _, _, turn_samples = _motion(samples, DEFAULT_MAX_SAMPLE_GAP_MIN)
    turns = [(rate, index[sample.sample_id]) for rate, sample in turn_samples
             if rate > 0.]
    events = []
    if approaches:
        events.append(min(approaches))
    distances = [(distance, i) for i, s in enumerate(samples)
                 if (distance := _visual_range(s)) is not None]
    if distances:
        events.append(min(distances)[1])
    if turns:
        events.append(max(turns, key=lambda pair: (pair[0], -pair[1]))[1])
    selected = []
    for i in (0, len(samples) - 1, *events, *(j for i in events for j in neighbors[i])):
        if 0 <= i < len(samples) and i not in selected and len(selected) < limit:
            selected.append(i)
    remaining = [i for i in range(len(samples)) if i not in selected]
    count = limit - len(selected)
    for i in range(1, count + 1):
        target = i * (len(samples) - 1) // (count + 1)
        chosen = min(remaining, key=lambda j: (abs(j - target), j))
        selected.append(chosen)
        remaining.remove(chosen)
    return tuple(samples[i] for i in sorted(selected))
