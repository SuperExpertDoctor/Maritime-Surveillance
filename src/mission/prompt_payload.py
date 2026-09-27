from copy import deepcopy


PROMPT_FORMAT_VERSION = "mission-prompt/v2"
UAV_OPTION_COLUMNS = ("uav_id", "transit_time_min", "total_range_cells")


def encode_selection_payload(payload: dict) -> dict:
    wire = deepcopy(payload)
    wire.pop("instructions", None)
    wire["prompt_format_version"] = PROMPT_FORMAT_VERSION
    snapshot = wire["snapshot"]
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
    eligibility = {}
    for edge in snapshot.get("feasible_edges", []):
        edge["uav_options"] = [
            dict(zip(columns, row, strict=True)) for row in edge["uav_options"]
        ]
        eligibility[edge["task_id"]] = sorted(
            option["uav_id"] for option in edge["uav_options"]
        )
    for candidate in snapshot.get("candidates", []):
        if candidate["task_id"] in eligibility:
            candidate.setdefault("feasible_uav_ids", eligibility[candidate["task_id"]])
    return decoded
