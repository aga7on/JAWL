"""Cold-import regression tests for the Goal/skill protocol boundary."""

from __future__ import annotations

import subprocess
import sys


def test_skill_registry_can_cold_import_before_goal_package_exports():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.l3_agent.skills.registry import SkillResult; "
                "from src.l3_agent.goals import GoalManager, GoalSkills; "
                "assert SkillResult and GoalManager and GoalSkills"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
