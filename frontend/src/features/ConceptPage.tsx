import { useMemo, useState } from "react";
import { ArrowRight, Layers3, Search } from "lucide-react";
import type { Concept } from "../types";

export function ConceptPage({
  courseId,
  concepts,
  onLearn,
}: {
  courseId: string;
  concepts: Concept[];
  onLearn: (concept: Concept) => void;
}) {
  const [query, setQuery] = useState(""),
    [module, setModule] = useState("全部");
  const modules = useMemo(
    () => ["全部", ...new Set(concepts.map((c) => c.module))],
    [concepts],
  );
  const visible = concepts.filter(
    (c) =>
      (module === "全部" || c.module === module) &&
      c.name.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <div className="page-content">
      <div className="page-title">
        <span className="eyebrow">KNOWLEDGE MAP</span>
        <h1>看见知识之间的联系</h1>
        <p>
          收录 {concepts.length} 个课程
          {courseId === "math" ? "知识节点" : "知识点"}
          {courseId === "math"
            ? "，沿着知识分类与题目关联探索数学方法。"
            : "，从基础建模逐步进入有限元分析。"}
        </p>
      </div>
      <div className="search-field compact">
        <Search size={18} />
        <input
          aria-label="搜索知识点"
          placeholder="搜索知识点名称"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <div className="module-tabs">
        {modules.map((name) => (
          <button
            key={name}
            className={name === module ? "active" : ""}
            onClick={() => setModule(name)}
          >
            {name}
          </button>
        ))}
      </div>
      <div className="concept-library">
        {visible.map((c) => (
          <button
            className="concept-library-card"
            key={c.id}
            onClick={() => onLearn(c)}
          >
            <Layers3 size={19} />
            <div>
              <span className="eyebrow">{c.module}</span>
              <h3>{c.name}</h3>
              <p>{c.question_count} 道关联题目</p>
            </div>
            <ArrowRight size={17} />
          </button>
        ))}
      </div>
      <p className="small muted">
        {courseId === "math"
          ? "知识分类与题目关联来自 XES3G5M；保留原始节点名称和编号。"
          : "分组为课程整理结果；原始知识点和题目关联来自 MOOCRadar。"}
      </p>
    </div>
  );
}
