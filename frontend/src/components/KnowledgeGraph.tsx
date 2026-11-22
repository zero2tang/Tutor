import type { GraphData } from "../types";

export function KnowledgeGraph({
  data,
  onSelect,
}: {
  data: GraphData | null;
  onSelect?: (id: string) => void;
}) {
  if (!data?.nodes.length)
    return <div className="graph-placeholder">选择知识点后查看关联结构</div>;
  const nodes = [...data.nodes]
    .sort(
      (a, b) =>
        Number(data.seed_ids.includes(b.id)) -
        Number(data.seed_ids.includes(a.id)),
    )
    .slice(0, 10);
  const coordinates = new Map(
    nodes.map((node, index) => {
      const angle =
        ((index - 1) / Math.max(1, nodes.length - 1)) * Math.PI * 2 -
        Math.PI / 2;
      return [
        node.id,
        index === 0
          ? { x: 145, y: 118 }
          : { x: 145 + 104 * Math.cos(angle), y: 118 + 86 * Math.sin(angle) },
      ];
    }),
  );
  const edges = data.edges.filter(
    (edge) => coordinates.has(edge.source) && coordinates.has(edge.target),
  );
  const relationLabels: Record<
    string,
    { label: string; color: string; note: string }
  > = {
    HAS_CHILD: {
      label: "分类关系",
      color: "#7d9bb8",
      note: "上位与下位知识点",
    },
    RELATED_TO: {
      label: "同题关联",
      color: "#8fbaac",
      note: "在题目中共同出现",
    },
    PREREQUISITE_OF: { label: "先修关系", color: "#d89954", note: "学习条件" },
  };
  const relations = [...new Set(edges.map((edge) => edge.relation_type))];
  return (
    <div className="knowledge-graph">
      <svg
        viewBox="0 0 290 240"
        role="img"
        aria-label="当前知识点的分类与关联结构图"
      >
        {edges.map((edge, index) => {
          const a = coordinates.get(edge.source)!,
            b = coordinates.get(edge.target)!;
          return (
            <line
              key={index}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={relationLabels[edge.relation_type]?.color || "#b9d7d1"}
              strokeWidth="1.5"
              strokeDasharray={
                edge.relation_type === "HAS_CHILD" ? "4 3" : undefined
              }
            />
          );
        })}
        {nodes.map((node, index) => {
          const point = coordinates.get(node.id)!;
          return (
            <g
              key={node.id}
              onClick={() => onSelect?.(node.id)}
              tabIndex={0}
              role="button"
              aria-label={`选择${node.name}`}
              onKeyDown={(e) => e.key === "Enter" && onSelect?.(node.id)}
              className="graph-node"
            >
              <circle
                cx={point.x}
                cy={point.y}
                r={index === 0 ? 23 : 16}
                fill={index === 0 ? "#176b63" : "#e8f3ef"}
                stroke={index === 0 ? "#176b63" : "#a9cfc2"}
              />
              <circle
                cx={point.x}
                cy={point.y}
                r={4}
                fill={index === 0 ? "#fff" : "#4d8a79"}
              />
              <text
                x={point.x}
                y={point.y + (index === 0 ? 39 : 29)}
                textAnchor="middle"
                fill="#3a5751"
                fontSize="10"
              >
                {node.name.length > 9 ? node.name.slice(0, 8) + "…" : node.name}
              </text>
            </g>
          );
        })}
      </svg>
      {relations.map((relation) => (
        <div className="graph-legend" key={relation}>
          <i
            style={{ background: relationLabels[relation]?.color || "#b9d7d1" }}
          />
          {relationLabels[relation]?.label || "知识关系"}
          <span>{relationLabels[relation]?.note || "来自课程知识结构"}</span>
        </div>
      ))}
    </div>
  );
}
