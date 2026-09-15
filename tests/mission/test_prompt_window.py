from types import SimpleNamespace

from src.mission.prompt_window import CandidatePool, PromptWindow


def _task(task_id, utility, bbox, *, kind="search", eligible=0.0):
    return SimpleNamespace(
        task_id=task_id,
        kind=kind,
        utility=float(utility),
        bbox=tuple(bbox),
        eligible_since_min=float(eligible),
        urgent=False,
    )


def test_stable_candidate_enters_prompt_within_skip_bound():
    tasks = tuple(
        _task(f"S{index}", 1.0 - index / 100.0,
              (index % 10, (index // 10) * 5, index % 10 + 2, (index // 10) * 5 + 2))
        for index in range(10)
    )
    window = PromptWindow()
    seen = set()
    fair_quota = 2
    bound = 5
    for cycle in range(bound):
        seen.update(task.task_id for task in window.select(tasks, 8, cycle).tasks)
    assert seen == {task.task_id for task in tasks}
    assert window.fairness_bound_cycles(len(tasks), fair_quota) == bound


def test_window_uses_utility_fairness_and_geography_without_duplicates():
    tasks = tuple(_task(
        f"S{index}", 100 - index,
        (index, (index % 4) * 5, index + 1, (index % 4) * 5 + 1),
        eligible=index,
    ) for index in range(12))
    selection = PromptWindow().select(tasks, 8, cycle=0)

    assert len(selection.tasks) == 8
    assert len({task.task_id for task in selection.tasks}) == 8
    assert set(selection.sources.values()) <= {"utility", "fair", "geography"}
    assert {"utility", "fair", "geography"} <= set(selection.sources.values())


def test_urgent_tasks_are_retained_before_search_quota():
    urgent = _task("H1", 0.0, (4, 4, 5, 5), kind="handoff")
    probe = _task("P1", 0.0, (6, 6, 7, 7), kind="probe")
    ordinary = tuple(_task(f"S{index}", index, (index, 0, index + 1, 1))
                     for index in range(8))

    selection = PromptWindow().select((ordinary[0], urgent, *ordinary[1:], probe), 4, 0)

    assert {task.task_id for task in selection.tasks[:2]} == {"H1", "P1"}


def test_candidate_pool_records_coverage_and_versions():
    pool = CandidatePool(
        candidates=(_task("S1", 1.0, (1, 1, 3, 3)),),
        unschedulable_cells=((8, 8),),
        geometry_version=4,
        information_version=9,
    )
    assert pool.geometry_version == 4
    assert pool.information_version == 9
    assert pool.unschedulable_cells == ((8, 8),)
