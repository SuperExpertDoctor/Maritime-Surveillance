"""LLM-backed contact identity assessment over validated observations only."""
from __future__ import annotations

from dataclasses import fields
import math
from pathlib import Path
from uuid import uuid4

from src.mission.config import ContactConfig
from src.mission.contracts import Assessment, ContactSnapshot, ProbeSession, TrajectoryFeatures
from src.mission.llm_gateway import LLMGateway
from src.mission.trajectory_features import build_features, select_keypoints


_PAYLOAD_KEYS = frozenset({
    "schema_version", "contact_id", "probe_id", "history_revision", "identity",
    "confidence", "evidence_sample_ids", "reasons", "alternative_explanations",
})
_FEATURE_KEYS = tuple(field.name for field in fields(TrajectoryFeatures))
_SAMPLE_KEYS = (
    "sample_id", "contact_id", "observed_at_min", "source", "source_id",
    "position_cells", "velocity_cells_min", "position_uncertainty_cells",
    "observer_position_cells", "measured_range_cells", "navigation_context",
)


def _visual_sample(sample, probe: ProbeSession) -> bool:
    return (
        sample.contact_id == probe.contact_id
        and sample.source in ("eo", "sar")
        and sample.source_id == probe.uav_id
        and sample.observer_position_cells is not None
        and sample.measured_range_cells is not None
    )


def _bounded_text_list(value, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 3:
        return (f"{name} must be a list of at most three strings",)
    if any(not isinstance(item, str) or not item or len(item) > 200 for item in value):
        return (f"{name} entries must be nonempty strings of at most 200 characters",)
    return ()


def validate_assessment_payload(payload: dict, contact: ContactSnapshot,
                                probe: ProbeSession,
                                features: TrajectoryFeatures) -> tuple[str, ...]:
    """Validate the untrusted model response against this observation revision."""
    errors = []
    if not isinstance(payload, dict):
        return ("assessment payload must be an object",)
    if set(payload) != _PAYLOAD_KEYS:
        errors.append("assessment payload has missing or unsupported keys")
    if payload.get("schema_version") != "contact-assessment/v1":
        errors.append("unsupported assessment schema")
    if payload.get("contact_id") != contact.contact_id:
        errors.append("assessment contact_id is not current")
    if payload.get("probe_id") != probe.probe_id:
        errors.append("assessment probe_id is not current")
    revision = payload.get("history_revision")
    if type(revision) is not int or revision != contact.revision:
        errors.append("assessment history_revision is not current")
    identity = payload.get("identity")
    if identity not in ("unknown", "target", "civilian") or not isinstance(identity, str):
        errors.append("assessment identity is invalid")
    confidence = payload.get("confidence")
    if (type(confidence) not in (int, float) or not math.isfinite(confidence)
            or not 0. <= confidence <= 1.):
        errors.append("assessment confidence must be finite and in [0, 1]")
    elif identity != "unknown" and confidence < .8:
        errors.append("terminal assessment confidence is below threshold")

    evidence = payload.get("evidence_sample_ids")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) for item in evidence):
        errors.append("evidence_sample_ids must be a nonempty list of strings")
    else:
        samples = {sample.sample_id: sample for sample in contact.samples}
        baseline_ids = set(features.baseline_sample_ids)
        near_ids = set(features.near_sample_ids)
        if len(evidence) != len(set(evidence)):
            errors.append("evidence_sample_ids must be unique")
        for sample_id in evidence:
            sample = samples.get(sample_id)
            if sample is None or not _visual_sample(sample, probe):
                errors.append("evidence must reference current visual evidence")
                break
        if identity != "unknown" and (
                not set(evidence) & baseline_ids or not set(evidence) & near_ids):
            errors.append("terminal assessment must cite baseline and near evidence")

    errors.extend(_bounded_text_list(payload.get("reasons"), "reasons"))
    errors.extend(_bounded_text_list(payload.get("alternative_explanations"), "alternative_explanations"))
    return tuple(errors)


class ContactAssessor:
    def __init__(self, *, gateway: LLMGateway, config: ContactConfig):
        self.gateway = gateway
        self.config = config
        self._attempted_revisions: set[tuple[str, int]] = set()
        self._last_assessed_at: dict[str, float] = {}
        self._prompt = Path(__file__).with_name("prompts").joinpath(
            "contact_assessor.txt").read_text(encoding="utf-8")

    def assess(self, contact: ContactSnapshot, probe: ProbeSession,
               features: TrajectoryFeatures, now_min: float) -> Assessment | None:
        if not self._eligible(contact, probe, features, now_min):
            return None
        revision_key = (contact.contact_id, contact.revision)
        if revision_key in self._attempted_revisions:
            return None
        if (contact.last_assessment is not None
                and contact.last_assessment.history_revision == contact.revision):
            return None
        last_at = self._last_assessed_at.get(contact.contact_id)
        if last_at is not None and now_min - last_at < self.config.assessment_interval_min:
            return None

        self._attempted_revisions.add(revision_key)
        result = self.gateway.request_json(
            role="contact_assessor",
            snapshot_id=f"{contact.contact_id}:{contact.revision}",
            system_prompt=self._prompt,
            user_payload=self._prompt_payload(contact, probe, features),
            validate=lambda payload: validate_assessment_payload(payload, contact, probe, features),
        )
        self._last_assessed_at[contact.contact_id] = now_min
        if not result.success or result.payload is None:
            return None
        payload = result.payload
        return Assessment(
            assessment_id=uuid4().hex,
            contact_id=contact.contact_id,
            probe_id=probe.probe_id,
            history_revision=contact.revision,
            assessed_at_min=now_min,
            identity=payload["identity"],
            confidence=float(payload["confidence"]),
            evidence_sample_ids=tuple(payload["evidence_sample_ids"]),
            reasons=tuple(payload["reasons"]),
            alternative_explanations=tuple(payload["alternative_explanations"]),
            model_call_id=result.call_id,
        )

    def _eligible(self, contact: ContactSnapshot, probe: ProbeSession,
                  features: TrajectoryFeatures, now_min: float) -> bool:
        if not math.isfinite(now_min) or contact.contact_id != probe.contact_id:
            return False
        if (contact.active_probe_id != probe.probe_id or contact.assigned_uav_id != probe.uav_id
                or probe.phase != "awaiting_assessment" or features.contact_id != contact.contact_id
                or features.probe_id != probe.probe_id or features.history_revision != contact.revision):
            return False
        if any(sample.observed_at_min > now_min for sample in contact.samples):
            return False
        try:
            expected = build_features(contact, probe, now_min, self.config)
        except (TypeError, ValueError):
            return False
        return features == expected and expected.sufficient_evidence

    def _prompt_payload(self, contact: ContactSnapshot, probe: ProbeSession,
                        features: TrajectoryFeatures) -> dict:
        def sample_payload(sample):
            return {key: getattr(sample, key) for key in _SAMPLE_KEYS}

        approach = [sample for sample in contact.samples if _visual_sample(sample, probe)]
        last = contact.last_assessment
        last_payload = None if last is None else {
            "identity": last.identity,
            "confidence": last.confidence,
            "evidence_sample_ids": list(last.evidence_sample_ids),
            "assessed_at_min": last.assessed_at_min,
        }
        return {
            "features": {key: getattr(features, key) for key in _FEATURE_KEYS},
            "keypoints": [sample_payload(sample) for sample in select_keypoints(
                contact.samples, self.config.prompt_keypoints_per_contact)],
            "uav_approach_history": [
                {key: getattr(sample, key) for key in (
                    "sample_id", "observed_at_min", "source_id", "observer_position_cells",
                    "measured_range_cells")}
                for sample in approach
            ],
            "last_assessment": last_payload,
            "map_context": {"near_land_fraction": features.near_land_fraction,
                            "confounders": list(features.confounders)},
        }


__all__ = ["ContactAssessor", "validate_assessment_payload"]
