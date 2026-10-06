from copy import deepcopy


PROMPT_FORMAT_VERSION = "mission-prompt/v2"
UAV_OPTION_COLUMNS = ("uav_id", "transit_time_min", "total_range_cells")

PAYLOAD_FLOAT_DIGITS = 4


def round_payload_floats(value):
    """Trim 15-digit float noise to millidegree precision in place.

    Model input only needs planning-grade precision; four decimals on
    cells/minutes are far below any decision threshold while saving
    roughly ten bytes per float on the wire.
    """
    if isinstance(value, float):
        return round(value, PAYLOAD_FLOAT_DIGITS)
    if isinstance(value, dict):
        for key, item in value.items():
            value[key] = round_payload_floats(item)
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = round_payload_floats(item)
        return value
    if isinstance(value, tuple):
        return [round_payload_floats(item) for item in value]
    return value


def _derived_sample_id(sample: dict) -> str | None:
    source_id = sample.get("source_id")
    if source_id is None or "observed_at_min" not in sample or "source" not in sample:
        return None
    return f"{sample['source'].upper()}:{source_id}:{float(sample['observed_at_min']).hex()}"


def _slim_samples(node) -> None:
    """Drop sample fields that are derivable or fixed defaults, in place."""
    if isinstance(node, dict):
        if "sample_id" in node and "position_cells" in node:
            if node["sample_id"] == _derived_sample_id(node):
                del node["sample_id"]
            if node.get("measured_range_cells") is None:
                node.pop("measured_range_cells", None)
            if node.get("navigation_context") == "unknown":
                node.pop("navigation_context", None)
        for item in node.values():
            _slim_samples(item)
    elif isinstance(node, list):
        for item in node:
            _slim_samples(item)


def _fatten_samples(node) -> None:
    """Restore fields _slim_samples removed, in place."""
    if isinstance(node, dict):
        if "observed_at_min" in node and "source" in node and "position_cells" in node:
            node.setdefault("measured_range_cells", None)
            node.setdefault("navigation_context", "unknown")
            if "sample_id" not in node:
                node["sample_id"] = _derived_sample_id(node)
        for item in node.values():
            _fatten_samples(item)
    elif isinstance(node, list):
        for item in node:
            _fatten_samples(item)


def encode_selection_payload(payload: dict) -> dict:
    wire = deepcopy(payload)
    wire.pop("instructions", None)
    wire["prompt_format_version"] = PROMPT_FORMAT_VERSION
    snapshot = wire["snapshot"]
    _slim_samples(snapshot)
    snapshot["uav_option_columns"] = list(UAV_OPTION_COLUMNS)
    eligibility = {}
    for edge in snapshot.get("feasible_edges", []):
        options = edge["uav_options"]
        if any(set(option) != set(UAV_OPTION_COLUMNS) for option in options):
            raise ValueError("unsupported UAV option fields")
        eligibility[edge["task_id"]] = sorted(option["uav_id"] for option in options)
        edge["uav_options"] = [
            [option[column] for column in UAV_OPTION_COLUMNS]
            for option in options
        ]
    for candidate in snapshot.get("candidates", []):
        ids = candidate.get("feasible_uav_ids")
        if ids is not None and ids == eligibility.get(candidate["task_id"]):
            del candidate["feasible_uav_ids"]
    return wire


def decode_selection_payload(payload: dict) -> dict:
    decoded = deepcopy(payload)
    version = decoded.pop("prompt_format_version", None)
    if version is None:
        return decoded
    if version != PROMPT_FORMAT_VERSION:
        raise ValueError("unsupported mission prompt format")
    snapshot = decoded["snapshot"]
    columns = snapshot.pop("uav_option_columns")
    if columns != list(UAV_OPTION_COLUMNS):
        raise ValueError("unsupported UAV option columns")
    snapshot.pop("uav_index", None)
    snapshot.pop("task_index", None)
    _fatten_samples(snapshot)
    eligibility = {}
    for edge in snapshot.get("feasible_edges", []):
        edge["uav_options"] = [
            dict(zip(columns, row, strict=True))
            for row in edge["uav_options"]
        ]
        eligibility[edge["task_id"]] = sorted(
            option["uav_id"] for option in edge["uav_options"]
        )
    for candidate in snapshot.get("candidates", []):
        if candidate["task_id"] in eligibility:
            candidate.setdefault("feasible_uav_ids", eligibility[candidate["task_id"]])
    return decoded
