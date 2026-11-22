"""复用现有只读工具，收集真实资料回执，模型不能自行登记引用。"""


class GuidanceTools:
    ALLOWED = {
        "read_skill",
        "search_teaching_resources",
        "read_teaching_resource",
        "get_concept_details",
        "rag_retrieve",
        "search_exercises",
    }

    def __init__(self, base, sources=(), read_resources=()):
        self.base = base
        self.sources = {item["id"]: dict(item) for item in sources}
        self.read_resources = set(read_resources)

    def for_role(self, role, *, handler=None):
        """只缩小真实工具集合，不复制工具Schema或名称分派。"""
        return [item for item in self.base.for_role(role) if item.name in self.ALLOWED]

    async def observe(self, name, arguments, result, context):
        """结果观察器只登记真实读取来源，不参与领域工具执行。"""
        if name == "get_concept_details":
            course = self.base.catalog.course(context.course_id)
            citations = [
                {
                    "id": result["id"],
                    "title": result["name"],
                    "source_ref": course.get("source_ref", "课程目录"),
                    "origin": "course_catalog",
                    "course_id": context.course_id,
                }
            ]
            citations.extend(
                {
                    key: material[key]
                    for key in ("id", "title", "source_ref", "origin")
                    if key in material
                }
                for material in result.get("materials", [])
                if material.get("id") and material.get("source_ref")
            )
            result = {**result, "citations": [*result.get("citations", []), *citations]}
        for citation in result.get("citations", []):
            if isinstance(citation, dict) and citation.get("id"):
                self.sources[citation["id"]] = dict(citation)
        if name == "read_teaching_resource":
            self.read_resources.add(arguments["resource_id"])
        return result
