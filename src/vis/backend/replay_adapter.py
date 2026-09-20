"""Normalize historical frames at the replay boundary."""

from copy import deepcopy

from src.mission.vessel_compat import (
    normalize_legacy_ais_enabled,
    normalize_legacy_vessel_class,
)


_VALID_SURVEILLANCE_STAGES = {"undetected", "detected", "probing", "tracking"}
_MISSING = object()
_LIST_DEFAULTS = (
    "uavs",
    "contacts",
    "ships",
    "events",
    "markers",
    "evidence",
    "intents",
    "intent_statuses",
    "intent_events",
    "search_regions",
    "track_regions",
    "obstacles",
    "bases",
)


def _normalize_revision(value: object) -> int:
    if type(value) is int and value >= 1:
        return value
    return 1


def normalize_replay_frame(frame: dict) -> dict:
    """Return a detached canonical copy of one historical frame."""
    if not isinstance(frame, dict):
        raise TypeError("replay frame must be an object")

    result = deepcopy(frame)
    result.setdefault("schema_version", "mission-frame/v2")
    result.setdefault("visual_schema_version", "mission-visual/v1")
    result["mode"] = "replay"
    result.setdefault("cycle", 0)
    result.setdefault("sim_time_min", 0.0)
    result.setdefault("frame_id", 0)
    result.setdefault("coverage_metrics", None)
    for key in _LIST_DEFAULTS:
        if not isinstance(result.get(key), list):
            result[key] = []

    normalized_uavs: list[dict] = []
    for raw in result["uavs"]:
        if not isinstance(raw, dict):
            raise TypeError("uavs entries must be objects")
        item = deepcopy(raw)
        # A legacy record cannot acquire a route from its current position.
        # Missing route fields remain explicitly empty at the replay boundary.
        if not isinstance(item.get("planned_path"), list):
            item["planned_path"] = []
        if not isinstance(item.get("mission_route"), list):
            item["mission_route"] = []
        normalized_uavs.append(item)
    result["uavs"] = normalized_uavs

    raw_vessels = result.get("scenario_vessels", [])
    if not isinstance(raw_vessels, list):
        raise TypeError("scenario_vessels must be a list")
    normalized: list[dict] = []
    for index, raw in enumerate(raw_vessels):
        if not isinstance(raw, dict):
            raise TypeError("scenario_vessels entries must be objects")

        item = deepcopy(raw)
        vessel_class = normalize_legacy_vessel_class(
            raw.get("vessel_class", raw.get("ship_type", "unknown"))
        )
        ais_value = raw.get("ais_enabled", _MISSING)
        if ais_value is _MISSING:
            ais_value = raw.get("ais_mode", _MISSING)
        if ais_value is _MISSING and isinstance(raw.get("ais"), dict):
            ais_value = raw["ais"].get("enabled", _MISSING)
        if ais_value is _MISSING:
            ais_enabled = vessel_class == "type_i"
        else:
            try:
                ais_enabled = normalize_legacy_ais_enabled(ais_value)
            except ValueError:
                ais_enabled = vessel_class == "type_i"

        stage = raw.get("surveillance_stage", "undetected")
        if stage not in _VALID_SURVEILLANCE_STAGES:
            stage = "undetected"
        item.pop("ais_mode", None)
        item.update({
            "scenario_entity_id": raw.get(
                "scenario_entity_id", raw.get("id", f"scenario-vessel-{index + 1}"),
            ),
            "revision": _normalize_revision(raw.get("revision", 1)),
            "position": deepcopy(raw.get("position", raw.get("position_cells", [0.0, 0.0]))),
            "vessel_class": vessel_class,
            "ais_enabled": ais_enabled,
            "ais_controllable": vessel_class == "type_ii",
            "surveillance_stage": stage,
        })
        normalized.append(item)

    result["scenario_vessels"] = normalized
    if "initial_vessel_count" not in result:
        result["initial_vessel_count"] = result.get(
            "configured_vessel_count", len(normalized),
        )
    result.setdefault("actual_vessel_count", len(normalized))
    result["vessel_mutation_allowed"] = False
    result.pop("configured_vessel_count", None)
    result.pop("editing_allowed", None)
    return result


__all__ = ["normalize_replay_frame"]
