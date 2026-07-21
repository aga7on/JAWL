from merge import deep_merge


def test_deep_merge_does_not_mutate_inputs():
    base = {"nested": {"left": 1}}
    override = {"nested": {"right": 2}}
    result = deep_merge(base, override)
    assert result == {"nested": {"left": 1, "right": 2}}
    result["nested"]["left"] = 99
    assert base == {"nested": {"left": 1}}
    assert override == {"nested": {"right": 2}}


def test_deep_merge_replaces_dict_with_scalar():
    assert deep_merge({"value": {"old": True}}, {"value": 4}) == {"value": 4}
