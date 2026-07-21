from merge import deep_merge


def test_deep_merge_preserves_nested_siblings():
    result = deep_merge(
        {"database": {"host": "localhost", "port": 5432}},
        {"database": {"port": 6432}},
    )
    assert result == {"database": {"host": "localhost", "port": 6432}}


def test_deep_merge_replaces_scalar():
    assert deep_merge({"enabled": False}, {"enabled": True}) == {"enabled": True}
