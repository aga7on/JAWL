"""Keep task fixtures out of JAWL's repository-wide pytest collection."""

import os


collect_ignore_glob = (
    []
    if os.environ.get("JAWL_CODING_TASK_EVAL") == "1"
    else [
        "fixtures/*/repo/tests/*.py",
        "fixtures/*/oracle/*.py",
    ]
)
