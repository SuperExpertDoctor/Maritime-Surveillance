"""LLM-backed vessel-class assessment over validated observations only."""
from __future__ import annotations

from dataclasses import fields
import math
from pathlib import Path
from uuid import uuid4

from src.mission.config import ContactConfig
from src.mission.contracts import (
    Assessment, ContactAssessment, ContactSnapshot, ProbeSession, TrajectoryFeatures,
)
from src.mission.llm_gateway import LLMGateway
from src.mission.trajectory_features import build_features, select_keypoints


_PAYLOAD_KEYS = frozenset({
    "schema_version", "contact_id", "probe_id", "history_revision", "vessel_class",
    "confidence", "evidence_sample_ids", "reasons", "alternative_explanations",
})
_FEATURE_KEYS = tuple(field.name for field in fields(TrajectoryFeatures))
_SAMPLE_KEYS = (
    "sample_id", "contact_id", "observed_at_min", "source", "source_id",
    "position_cells", "velocity_cells_min", "position_uncertainty_cells",
    "observer_position_cells", "measured_range_cells", "navigation_context",
)
_MAX_EVIDENCE_SAMPLE_IDS = 12


def _visual_sample(sample, probe: ProbeSession) -> bool:
    return (
        sample.contact_id == probe.contact_id
        and sample.source in ("eo", "sar")
        and sample.source_id == probe.uav_id
        and sample.observer_position_cells is not None
        and sample.measured_range_cells is not None
    )


def _current_evidence_samples(contact: ContactSnapshot, probe: ProbeSession,
                              features: TrajectoryFeatures):
    """Return phase-scoped visual observations for this assessment revision."""
    phase_ids = set(features.baseline_sample_ids) | set(features.near_sample_ids)
    return tuple(sample for sample in contact.samples
                 if sample.sample_id in phase_ids and _visual_sample(sample, probe))


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
    vessel_class = payload.get("vessel_class")
    if vessel_class not in ("unknown", "type_i", "type_ii") or not isinstance(vessel_class, str):
        errors.append("assessment vessel_class is invalid")
    confidence = payload.get("confidence")
    if (type(confidence) not in (int, float) or not math.isfinite(confidence)
            or not 0. <= confidence <= 1.):
        errors.append("assessment confidence must be finite and in [0, 1]")
    elif vessel_class != "unknown" and confidence < .8:
        errors.append("terminal assessment confidence is below threshold")

    evidence = payload.get("evidence_sample_ids")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) for item in evidence):
        errors.append("evidence_sample_ids must be a nonempty list of strings")
    else:
        eligible_ids = {sample.sample_id for sample in _current_evidence_samples(contact, probe, features)}
        baseline_ids = set(features.baseline_sample_ids)
        near_ids = set(features.near_sample_ids)
        if len(evidence) > _MAX_EVIDENCE_SAMPLE_IDS:
            errors.append(f"evidence_sample_ids must contain at most {_MAX_EVIDENCE_SAMPLE_IDS} entries")
        if len(evidence) != len(set(evidence)):
            errors.append("evidence_sample_ids must be unique")
        if not set(evidence) <= eligible_ids:
            errors.append("evidence must reference current eligible visual evidence")
        if vessel_class != "unknown" and (
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

    def assess(self, contact: ContactSnapshot, probe: ProbeSession | None = None,
               features: TrajectoryFeatures | None = None,
               now_min: float | None = None) -> Assessment | ContactAssessment | None:
        # The legacy four-argument call is still the gateway-backed contact
        # class workflow. A single evidence batch uses the new dual-
        # dimension, observation-only assessment contract.
        if probe is None and features is None and now_min is None:
            return self.assess_dimensions(contact)
        if probe is None or features is None or now_min is None:
            raise TypeError("legacy assessment requires contact, probe, features, and now_min")
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
            vessel_class=payload["vessel_class"],
            confidence=float(payload["confidence"]),
            evidence_sample_ids=tuple(payload["evidence_sample_ids"]),
            reasons=tuple(payload["reasons"]),
            alternative_explanations=tuple(payload["alternative_explanations"]),
            model_call_id=result.call_id,
        )

    @staticmethod
    def assess_dimensions(evidence) -> ContactAssessment:
        """Classify vessel class and activity from validated evidence families.

        This deterministic adapter is intentionally conservative: a type_ii
        signal can establish the vessel class, but activity requires two
        independent quality-gated families and at least one observed-motion
        family. AIS silence alone contributes to neither dimension.
        """
        items = tuple(evidence or ())

        def value(item, *names, default=None):
            if isinstance(item, dict):
                for name in names:
                    if name in item:
                        return item[name]
                return default
            for name in names:
                if hasattr(item, name):
                    return getattr(item, name)
            return default

        valid = [item for item in items if value(item, "passes_quality_gate", default=True)]
        class_ids: list[str] = []
        activity_ids: list[str] = []
        class_candidates: list[tuple[str, float, str]] = []
        families: set[str] = set()
        normalized_items: list[tuple[object, str]] = []
        for item in valid:
            evidence_id = value(item, "evidence_id", "id", default="")
            family = str(value(item, "family", "kind", default=""))
            family = {
                "type_ii_assessment": "radiation_activity",
                "violation_assessment": "violation_activity",
                "eo_class": "class",
                "sar_class": "class",
            }.get(family, family)
            families.add(family)
            normalized_items.append((item, family))
            explicit_class = value(item, "vessel_class", "classification", "class_label")
            confidence = value(item, "confidence", "strength", default=1.0)
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
            except (TypeError, ValueError):
                confidence = 0.0
            if explicit_class in ("type_i", "type_ii"):
                class_candidates.append((explicit_class, confidence, evidence_id))
            if family in {"eo_class", "class"}:
                class_ids.append(evidence_id)
            if family in {"eo_activity", "survey_motion", "radiation_activity", "violation_activity"}:
                activity_ids.append(evidence_id)

        if class_candidates:
            vessel_class, class_confidence, _ = max(
                class_candidates, key=lambda item: (item[1], item[2])
            )
        else:
            vessel_class, class_confidence = "unknown", 0.0

        independent_activity = families & {
            "eo_activity", "survey_motion", "radiation_activity", "violation_activity",
        }
        has_motion_family = bool(independent_activity & {"eo_activity", "survey_motion"})
        if len(independent_activity) >= 2 and has_motion_family:
            activity = "confirmed_violation"
            activity_confidence = min(
                1.0, sum(
                    float(value(item, "confidence", "strength", default=1.0))
                    for item, family in normalized_items
                    if family in independent_activity
                ) / len(independent_activity)
            )
        elif independent_activity:
            activity = "suspected_violation"
            activity_confidence = 0.5
        else:
            activity = "unknown"
            activity_confidence = 0.0
        return ContactAssessment(
            vessel_class=vessel_class,
            class_confidence=class_confidence,
            class_evidence_ids=tuple(item for item in class_ids if item),
            activity=activity,
            activity_confidence=activity_confidence,
            activity_evidence_ids=tuple(item for item in activity_ids if item),
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

        approach = select_keypoints(
            _current_evidence_samples(contact, probe, features),
            self.config.prompt_keypoints_per_contact,
        )
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
            "map_context": {"near_land_fraction": features.near_land_fraction,
                            "confounders": list(features.confounders)},
        }


__all__ = ["ContactAssessor", "validate_assessment_payload"]
