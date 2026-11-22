import { useEffect, useState } from "react";
import { QuestionCard } from "./QuestionCard";
import type { ExerciseBatch } from "../types";

export interface BatchSubmission {
  batch_id: string;
  answers: { question_id: string; answer: string }[];
  request_id: string;
}

export function ExerciseBatchCard({
  batch,
  pending,
  busy,
  onSubmit,
  onExplain,
  onSkip,
  submittedAnswers,
}: {
  batch: ExerciseBatch;
  pending: boolean;
  busy: boolean;
  onSubmit: (submission: BatchSubmission) => void;
  onExplain: (index: number) => void;
  onSkip: () => void;
  submittedAnswers?: Record<string, string>;
}) {
  const storageKey = `stem-tutor-batch-draft:${batch.id}`;
  const [draft, setDraft] = useState<{
    answers: Record<string, string>;
    key: string;
  }>(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      if (
        saved &&
        typeof saved.key === "string" &&
        saved.answers &&
        Object.values(saved.answers).every(
          (answer) => typeof answer === "string",
        )
      )
        return saved;
    } catch {
      /* 草稿损坏不阻塞练习。 */
    }
    return { answers: {}, key: crypto.randomUUID() };
  });
  useEffect(() => {
    try {
      if (pending) sessionStorage.setItem(storageKey, JSON.stringify(draft));
      else sessionStorage.removeItem(storageKey);
    } catch {
      /* 浏览器禁用存储时仍可在当前页面完成作答。 */
    }
  }, [draft, pending, storageKey]);
  const answers = pending ? draft.answers : submittedAnswers || draft.answers;
  const completed = batch.questions.filter((question) =>
    answers[question.id]?.trim(),
  ).length;
  return (
    <section className="exercise-batch-card" aria-label={batch.title}>
      <div className="batch-heading">
        <h3>{batch.title}</h3>
        <span>
          {pending
            ? `已完成 ${completed} / ${batch.questions.length}`
            : `共 ${batch.questions.length} 题`}
        </span>
      </div>
      {batch.questions.map((question, index) => (
        <div key={question.id} className="batch-question">
          <strong>第 {index + 1} 题</strong>
          {question.difficulty_target && (
            <p className="context-note">
              目标难度 {question.difficulty_target.minimum}–
              {question.difficulty_target.maximum} / 100 · 实际难度待独立标注
            </p>
          )}
          <QuestionCard
            question={question}
            pending={pending}
            busy={busy}
            grouped
            draft={{
              value: answers[question.id] || "",
              onChange: (value) =>
                setDraft((previous) => ({
                  answers: { ...previous.answers, [question.id]: value },
                  key: crypto.randomUUID(),
                })),
            }}
            onAnswer={() => {}}
            onExplain={() => onExplain(index)}
            onSkip={onSkip}
          />
          {pending && (
            <button
              className="text-button"
              disabled={busy}
              onClick={() => onExplain(index)}
            >
              查看本题讲解
            </button>
          )}
          {batch.assisted_question_ids.includes(question.id) && (
            <p className="assisted-note">本题已获辅助，不计入独立掌握证据。</p>
          )}
          {!pending && answers[question.id] && (
            <p className="context-note">你的作答：{answers[question.id]}</p>
          )}
        </div>
      ))}
      {pending && (
        <div className="batch-submit">
          <p>全部作答后统一评估，每道题分别保存作答证据。</p>
          <button
            className="primary"
            disabled={busy || completed !== batch.questions.length}
            onClick={() =>
              onSubmit({
                batch_id: batch.id,
                request_id: draft.key,
                answers: batch.questions.map((question) => ({
                  question_id: question.id,
                  answer: draft.answers[question.id],
                })),
              })
            }
          >
            {busy ? "正在处理…" : `提交全部 ${batch.questions.length} 题`}
          </button>
          <button className="text-button" disabled={busy} onClick={onSkip}>
            跳过整组
          </button>
        </div>
      )}
    </section>
  );
}
