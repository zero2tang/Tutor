import { LoaderCircle } from "lucide-react";
import type { StreamingDraft } from "../streaming/drafts";
import { Markdown } from "./Markdown";

const agentNames = {
  supervisor: "学习决策",
  knowledge: "知识点学习",
  exercise: "习题讲解",
  assessment: "学习评估",
};

export function StreamingMessage({ draft }: { draft: StreamingDraft }) {
  const { fields } = draft;
  return (
    <article
      className="message assistant-message streaming-message"
      aria-busy="true"
    >
      <div className="message-identity">
        <div className="agent-avatar">理</div>
        <strong>理序</strong>
        <span>{agentNames[draft.agent]}</span>
        <span className="streaming-status">
          <LoaderCircle size={12} className="spin" />
          生成中
        </span>
      </div>
      <div className="message-body">
        {fields.title && <h2 className="streaming-title">{fields.title}</h2>}
        {(["message", "content", "feedback", "summary"] as const).map(
          (field) =>
            fields[field] ? (
              <Markdown key={field}>{fields[field]}</Markdown>
            ) : null,
        )}
        {fields.follow_up && (
          <div className="streaming-follow-up">
            <span>接下来</span>
            <Markdown>{fields.follow_up}</Markdown>
          </div>
        )}
        <span className="streaming-cursor" aria-hidden="true" />
      </div>
    </article>
  );
}
