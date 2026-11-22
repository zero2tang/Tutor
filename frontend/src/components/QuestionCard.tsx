import { useState } from "react";
import { ArrowRight, Check, Flag, Lightbulb } from "lucide-react";
import { Markdown } from "./Markdown";
import { DifficultyBadge } from "./DifficultyBadge";
import type { Question } from "../types";

const typeNames: Record<string, string> = {
  choice: "单选题",
  multi_choice: "多选题",
  fill: "填空题",
};
export function QuestionCard({
  question,
  pending,
  busy,
  assisted,
  onAnswer,
  onExplain,
  onSkip,
  draft,
  grouped = false,
}: {
  question: Question;
  pending: boolean;
  busy: boolean;
  assisted?: boolean;
  onAnswer: (value: string) => void;
  onExplain: () => void;
  onSkip: () => void;
  draft?: { value: string; onChange: (value: string) => void };
  grouped?: boolean;
}) {
  const [localSelected, setSelected] = useState<string[]>([]);
  const [localAnswer, setAnswer] = useState("");
  const selected = draft
    ? draft.value.split(",").filter(Boolean)
    : localSelected;
  const answer = draft ? draft.value : localAnswer;
  const submit = () => {
    const value = Object.keys(question.options).length
      ? selected.join(",")
      : answer.trim();
    if (value) onAnswer(value);
  };
  return (
    <section className="question-card">
      <div className="question-heading">
        <span className="eyebrow">独立练习</span>
        <span className="tag">
          {typeNames[question.question_type] || question.question_type}
        </span>
        {question.generated && <span className="tag warm">生成变式</span>}
        <DifficultyBadge question={question} />
        {question.question_type === "fill" &&
          (question.blank_count || 0) > 1 && (
            <span className="tag">{question.blank_count} 空 · 按顺序填写</span>
          )}
      </div>
      <Markdown>{question.stem}</Markdown>
      {!!Object.keys(question.options).length && (
        <div className="options">
          {Object.entries(question.options).map(([label, text]) => (
            <button
              key={label}
              disabled={!pending || busy}
              className={`option ${selected.includes(label) ? "selected" : ""}`}
              onClick={() => {
                const previous = selected;
                const next =
                  question.question_type === "multi_choice"
                    ? previous.includes(label)
                      ? previous.filter((x) => x !== label)
                      : [...previous, label]
                    : [label];
                if (draft) draft.onChange(next.join(","));
                else setSelected(next);
              }}
            >
              <span className="option-letter">
                {selected.includes(label) ? <Check size={14} /> : label}
              </span>
              <Markdown>{text}</Markdown>
            </button>
          ))}
        </div>
      )}
      {pending && Object.keys(question.options).length === 0 && (
        <textarea
          className="fill-answer"
          aria-label="填写答案"
          placeholder="填写答案；多个空请用分号分隔"
          value={answer}
          disabled={busy}
          onChange={(event) =>
            draft
              ? draft.onChange(event.target.value)
              : setAnswer(event.target.value)
          }
        />
      )}
      {pending && !grouped && (
        <>
          <div className="question-actions">
            <button
              className="primary"
              disabled={busy || (!selected.length && !answer.trim())}
              onClick={submit}
            >
              提交答案
              <ArrowRight size={16} />
            </button>
            <button className="text-button" disabled={busy} onClick={onExplain}>
              <Lightbulb size={15} />
              查看讲解
            </button>
            <button className="text-button" disabled={busy} onClick={onSkip}>
              跳过
            </button>
          </div>
          {assisted && (
            <p className="assisted-note">
              <Flag size={13} />
              已查看讲解，本次记录为辅助练习。
            </p>
          )}
        </>
      )}
      {!pending && (
        <div className="question-finished">
          <Check size={14} />
          本次练习已结束
        </div>
      )}
      <div className="source-line">{question.source_ref}</div>
    </section>
  );
}
