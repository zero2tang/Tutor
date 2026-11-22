import { GitBranch } from "lucide-react";
import type { SessionGoals as GoalState } from "../types";

const statusNames: Record<string, string> = {
  planned: "待开始", active: "进行中", blocked: "先补充基础",
  awaiting_student: "等待回答", completed: "活动已完成", cancelled: "已取消",
};

export function SessionGoals({ state }: { state: GoalState | null | undefined }) {
  if (!state) return null;
  const nodes = state.nodes.filter(node => node.goal_id !== state.root_goal_id && node.status !== "cancelled");
  const point = state.return_stack.at(-1);
  const returnNode = state.nodes.find(node => node.goal_id === point?.goal_id);
  return (
    <section className="context-card" aria-label="本会话目标与子目标">
      <div className="card-heading"><GitBranch size={16} /><h3>本会话目标与子目标</h3></div>
      {nodes.slice(-8).map(node => (
        <div className="mini-mastery" key={node.goal_id}>
          <div><strong>{node.description}</strong><span>{statusNames[node.status]}</span></div>
          {node.goal_id === state.active_goal_id && node.success_criteria[0] && (
            <p className="context-note">完成标准：{node.success_criteria[0]}</p>
          )}
        </div>
      ))}
      {!nodes.length && <p className="context-note">当前围绕整体目标确认具体学习步骤。</p>}
      {point && <p className="context-note">补充学习后返回：{returnNode?.description || point.current_step || "原题原步骤"}</p>}
      {state.next_action && <p className="context-note">接下来：{state.next_action}</p>}
      <p className="muted small">目标属于当前会话；活动完成与知识掌握分别记录。</p>
    </section>
  );
}
