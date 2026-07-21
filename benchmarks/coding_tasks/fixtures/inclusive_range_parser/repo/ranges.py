def parse_range(spec: str) -> list[int]:
    """Parse a numeric ``start-end`` range."""

    start_text, end_text = spec.split("-", maxsplit=1)
    start = int(start_text)
    end = int(end_text)
    return list(range(start, end))
