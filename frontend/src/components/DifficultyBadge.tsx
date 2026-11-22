import type { Question } from "../types";

export function DifficultyBadge({ question }: { question: Question }) {
  if (typeof question.difficulty_value !== "number") return null;
  return (
    <span
      className="tag"
      title="按题目内容量表估计，0–100越大越难，不是答错概率"
    >
      估计难度 {Number(question.difficulty_value.toFixed(2))}/100
    </span>
  );
}
