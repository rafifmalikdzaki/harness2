"""Tests for how a selector's answer is read.

The selector answers with an anonymous id, because showing it the real
labels would let it pick by name rather than by evidence. A model that
answers with a real label anyway has still made a choice, and if that label
is in the pool the choice is unambiguous -- so it is accepted rather than
discarded.

That matters for the general path, where the pool includes the reference
executions: their labels (`base1`, `base2`) are short and guessable, so a
model reaching for a label rather than an id is likelier there.
"""


from harness2.core.selection import Selection, assign_ids


def test_ids_are_assigned_over_every_pool_label():
    id_map = assign_ids(["agg1", "agg2", "base1"], task_id="t", k=2, mode="agg")
    assert sorted(id_map) == ["A", "B", "C"]
    assert sorted(id_map.values()) == ["agg1", "agg2", "base1"]


def test_the_same_pool_always_gets_the_same_ids():
    first = assign_ids(["agg1", "agg2", "base1"], task_id="t", k=2, mode="agg")
    second = assign_ids(["agg1", "agg2", "base1"], task_id="t", k=2, mode="agg")
    assert first == second


def test_a_label_is_mappable_back_to_its_id():
    """The inverse of the mapping a selector's answer needs."""
    id_map = assign_ids(["agg1", "agg2", "base1"], task_id="t", k=2, mode="agg")
    label = "base1"
    anon = next(a for a, lbl in id_map.items() if lbl == label)
    assert anon in id_map
    assert id_map[anon] == label


def test_selection_records_the_choice():
    sel = Selection(task_id="t", mode="agg", k=2)
    sel.chosen_id = "A"
    sel.chosen_label = "agg1"
    assert sel.chosen_id == "A"
    assert sel.chosen_label == "agg1"


def test_the_pool_can_hold_reference_executions():
    """The general path selects among the finals *and* the references."""
    from harness2.core.selection import selection_pool

    class Run:
        def __init__(self, label):
            self.label = label

    rollouts = {"agg1": Run("agg1"), "agg2": Run("agg2")}
    base = [Run("base1"), Run("base2")]

    finals = selection_pool("agg", 2, rollouts, base)
    assert sorted(finals) == ["agg1", "agg2"]

    with_base = selection_pool("agg", 2, rollouts, base, include_base=True)
    assert sorted(with_base) == ["agg1", "agg2", "base1", "base2"]
