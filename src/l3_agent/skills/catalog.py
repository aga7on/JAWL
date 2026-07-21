"""Bounded hierarchical discovery for large runtime skill registries."""

import json

from src.l3_agent.skills.registry import SkillResult, search_skill_docs, skill


class SkillCatalog:
    """Expose exact skill signatures without injecting the full catalogue each turn."""

    @skill()
    async def search_skills(self, query: str, limit: int = 12) -> SkillResult:
        """Search available skill names/descriptions and return exact signatures. Use this when the compact context lists a namespace but omits the required tool."""
        try:
            matches = search_skill_docs(query, limit=limit)
        except ValueError as exc:
            return SkillResult.fail(str(exc))
        return SkillResult.ok(
            json.dumps(
                {"query": query, "count": len(matches), "skills": matches},
                ensure_ascii=False,
            )
        )
