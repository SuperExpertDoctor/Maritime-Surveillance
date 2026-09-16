"""Explicit adapters for legacy vessel and AIS input values.

No new runtime output should call these helpers.  They exist at input and
replay boundaries only, so compatibility cannot leak back into canonical
contracts.
"""

from copy import deepcopy
from typing import Mapping

from src.mission.contracts import VesselClass


_LEGACY_CLASS = {
    "civilian": "type_i",
    "research": "type_ii",
    "military": "type_ii",
    "target": "type_ii",
}


def normalize_legacy_vessel_class(value: str) -> VesselClass:
    if not isinstance(value, str):
        raise ValueError(f"unsupported vessel class: {value}")
    normalized = _LEGACY_CLASS.get(value, value)
    if normalized not in {"unknown", "type_i", "type_ii"}:
        raise ValueError(f"unsupported vessel class: {value}")
    return normalized  # type: ignore[return-value]


def normalize_legacy_ais_enabled(value: object) -> bool:
    if type(value) is bool:
        return value
    if value == "civilian":
        return True
    if value == "silent":
        return False
    raise ValueError("unsupported legacy AIS state")


def normalize_legacy_ship_config(mapping: Mapping[str, object]) -> dict:
    result = deepcopy(dict(mapping))
    population = dict(result.get("population", {}))
    if "civilian_ratio" in population:
        if "type_i_ratio" in population:
            raise ValueError("population contains both civilian_ratio and type_i_ratio")
        population["type_i_ratio"] = population.pop("civilian_ratio")
    if "research_ratio" in population:
        if "type_ii_ratio" in population:
            raise ValueError("population contains both research_ratio and type_ii_ratio")
        population["type_ii_ratio"] = population.pop("research_ratio")
    if "target_ais_on_probability" in result:
        if "type_ii_ais_on_probability" in result:
            raise ValueError(
                "ship contains both target_ais_on_probability and "
                "type_ii_ais_on_probability"
            )
        result["type_ii_ais_on_probability"] = result.pop(
            "target_ais_on_probability"
        )
    result["population"] = population
    return result


__all__ = [
    "normalize_legacy_ais_enabled",
    "normalize_legacy_ship_config",
    "normalize_legacy_vessel_class",
]
