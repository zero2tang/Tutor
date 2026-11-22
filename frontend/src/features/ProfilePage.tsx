import {
  BookCheck,
  ChartNoAxesCombined,
  Clock3,
  RefreshCw,
  Trash2,
} from "lucide-react";
import type { Profile } from "../types";
import { Markdown } from "../components/Markdown";
import { ScopedMemoryView } from "../components/ScopedMemoryView";

const levels: Record<string, string> = {
  beginner: "基础",
  advanced: "进阶",
  proficient: "熟练",
};
export function ProfilePage({
  profile,
  onRefresh,
  onForget,
}: {
  profile: Profile | null;
  onRefresh: () => void;
  onForget: (id: string) => void;
}) {
  const assessed =
    profile?.states.filter((state) => state.status !== "unassessed") || [];
  return (
    <div className="page-content">
      <div className="page-title title-row">
        <div>
          <span className="eyebrow">LEARNING RECORD</span>
          <h1>每一次进步，都有依据</h1>
          <p>
            {profile?.step_learning
              ? "分别记录辅助学习与独立新题表现，长期保持仍需复测。"
              : "从真实作答积累证据，分别观察基础、进阶和熟练层级。"}
          </p>
        </div>
        <button className="secondary" onClick={onRefresh}>
          <RefreshCw size={15} />
          刷新档案
        </button>
      </div>
      {profile?.step_learning && (
        <section className="learning-evidence-card">
          <h2>历史方程学习档案（固定快照）</h2>
          <p>
            独立新题验证：{profile.step_learning.independent_transfer_count} 题
            · 辅助完成：{profile.step_learning.assisted_completion_count} 题
          </p>
          <p>
            已记录 {profile.step_learning.observed_steps} 次分步提交；待复核题目{" "}
            {profile.step_learning.pending_review_count} 道。
          </p>
          <p className="muted">{profile.step_learning.note}</p>
          {profile.step_learning.evidence.map((item) => (
            <p key={item.id}>
              {item.assisted
                ? "辅助完成"
                : item.independent
                  ? "独立完成"
                  : "复做旧题，未获帮助"}{" "}
              ·{" "}
              {item.scope === "final_answer_only"
                ? "仅观察到最终答案"
                : "有分步作答记录"}
              {item.simulated ? " · 模拟验收记录" : ""}
            </p>
          ))}
        </section>
      )}
      {!profile?.step_learning && (
        <div className="stat-grid">
          <div className="stat-card">
            <BookCheck />
            <strong>{profile?.attempt_count || 0}</strong>
            <span>已提交练习</span>
          </div>
          <div className="stat-card">
            <ChartNoAxesCombined />
            <strong>{profile?.assessed_concepts || 0}</strong>
            <span>已有评估的知识点</span>
          </div>
          <div className="stat-card">
            <Clock3 />
            <strong>{profile?.correct_count || 0}</strong>
            <span>正确作答记录</span>
          </div>
        </div>
      )}
      <h2 className="section-title">知识点状态</h2>
      {!assessed.length && (
        <div className="empty-state">
          <ChartNoAxesCombined size={32} />
          <h3>
            {profile?.step_learning
              ? "细分技能的掌握状态仍待评估"
              : "从第一道练习开始"}
          </h3>
          <p>
            {profile?.step_learning
              ? "正确完成方程不等于已经证明每个细分技能都掌握，先保留上述作答证据。"
              : "尚未评估的知识点保持未知，完成练习后将在这里显示证据。"}
          </p>
        </div>
      )}
      <div className="mastery-list">
        {assessed.map((state) => (
          <section className="mastery-card" key={state.concept_id}>
            <div>
              <h3>{state.name}</h3>
              <span className="muted small">
                {state.evidence_count} 道独立题目 ·{" "}
                {state.status === "mastered"
                  ? "已达到掌握门槛"
                  : "继续积累证据"}
              </span>
            </div>
            <div className="level-grid">
              {Object.entries(levels).map(([key, label]) => (
                <div key={key}>
                  <div className="level-caption">
                    <span>{label}</span>
                    <strong>
                      {state.mastery[key] === null
                        ? "待评估"
                        : Math.round(state.mastery[key]! * 100) + "%"}
                    </strong>
                  </div>
                  <div className="progress-bar">
                    <i
                      style={{ width: `${(state.mastery[key] || 0) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
            {state.misconceptions.length > 0 && (
              <div className="misconception">
                <Markdown>{`**诊断观察**：${state.misconceptions[state.misconceptions.length - 1]}`}</Markdown>
              </div>
            )}
          </section>
        ))}
      </div>
      <h2 className="section-title">
        学习记忆<span>模型根据学习记录提取，可在会话中整理</span>
      </h2>
      {profile?.memory_schema === "business-memory-v2" && <ScopedMemoryView memory={profile} />}
      {profile?.memory_schema !== "business-memory-v2" && profile?.student_memory?.content && (
        <section className="memory-card"><div><span className="tag warm">长期学生记忆</span><Markdown>{profile.student_memory.content}</Markdown></div></section>
      )}
      {profile?.session_experiences?.map(item => (
        <section className="memory-card" key={item.session_id}><div><span className="tag warm">会话学习经历 · {item.status === "final" ? "已整理" : "更新中"}</span><Markdown>{item.content}</Markdown></div></section>
      ))}
      <div className="memory-list">
        {profile?.memories.map((memory) => (
          <div key={memory.id} className="memory-card">
            <div>
              <span className="tag warm">错误模式</span>
              <Markdown>{memory.text}</Markdown>
            </div>
            <button
              className="icon-button"
              aria-label="忘记这条记忆"
              onClick={() => onForget(memory.id)}
            >
              <Trash2 size={16} />
            </button>
          </div>
        ))}
        {!profile?.memories.length && !profile?.student_memory?.content && !profile?.session_experiences?.length && !profile?.global_memory?.content && !profile?.course_memory?.content && !profile?.concept_memories?.some(item => item.content) && (
          <p className="muted">尚未提取学习记忆，原始学习记录会继续保留。</p>
        )}
      </div>
    </div>
  );
}
