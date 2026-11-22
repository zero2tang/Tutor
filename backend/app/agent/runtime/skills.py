"""技能先展示用途，模型需要时再读取正文，不扩展角色的工具权限。"""

from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills"


class SkillLibrary:
    def __init__(self, root: Path = SKILL_ROOT):
        """指定角色技能目录；运行时默认读取 app/agent/skills 下的 Markdown 文件。"""
        self.root = root

    def catalog(self, role: str) -> list[dict]:
        """只返回本角色的技能名、标题和简介，供模型判断是否需要加载正文。"""
        if role not in {"supervisor", "knowledge", "exercise", "assessment"}:
            raise ValueError("未知的业务角色")
        items = []
        for path in sorted((self.root / role).glob("*.md")):
            lines = [
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            items.append(
                {
                    "name": path.stem,
                    "title": lines[0].lstrip("# ") if lines else path.stem,
                    "description": lines[1][:240] if len(lines) > 1 else "按需读取专业处理方法。",
                }
            )
        return items

    def read(self, role: str, name: str) -> dict:
        """读取本角色目录中已登记的技能全文；技能内容不会扩大角色的工具权限。"""
        # 文件名必须来自本角色的目录，拒绝路径穿越和跨角色加载。
        allowed = {item["name"] for item in self.catalog(role)}
        if name not in allowed:
            raise ValueError("技能不存在或不属于当前角色，请使用技能目录中的 name")
        return {
            "name": name,
            "role": role,
            "instructions": (self.root / role / (name + ".md")).read_text(encoding="utf-8"),
        }
