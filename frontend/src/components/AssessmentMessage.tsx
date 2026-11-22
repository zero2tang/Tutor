import type { Assessment, Concept } from "../types";
import { Markdown } from "./Markdown";

const labels: Record<string, string> = {
  correct: "回答正确",
  incorrect: "需要再巩固",
  partial: "部分正确",
  ungraded: "需要补充说明",
};
const practiceLevels: Record<string, string> = {
  beginner: "记忆",
  advanced: "理解",
  proficient: "应用",
};
const terms: Record<string, string> = {
  beginner: "基础",
  advanced: "进阶",
  proficient: "熟练",
  correct: "正确",
  incorrect: "错误",
  partial: "部分正确",
  ungraded: "待补充",
};

export function AssessmentMessage({
  assessment,
  content,
  concepts,
}: {
  assessment: Assessment;
  content: string;
  concepts: Concept[];
}) {
  // 原始诊断留在审计记录；学习界面把内部层级和知识点编号换成可读名称。
  const readable = (text: string) =>
    text
      .replace(
        /requested_level\s*=\s*(beginner|advanced|proficient)/g,
        (_, level: string) => `本次练习目标为${practiceLevels[level]}`,
      )
      .replace(
        /(?:mechanics|math):c\d+/g,
        (id) => concepts.find((c) => c.id === id)?.name || "当前知识点",
      )
      .replace(
        /\b(beginner|advanced|proficient|correct|incorrect|partial|ungraded)\b/g,
        (value) => terms[value],
      );
  const next = content.split("**下一步**：")[1] || "";
  return (
    <>
      <Markdown>{`## ${labels[assessment.result] || "学习反馈"}\n\n${readable(assessment.diagnosis.feedback)}${next ? `\n\n**下一步**：${readable(next)}` : ""}`}</Markdown>
      <details className="citation-list">
        <summary>查看评估依据</summary>
        <Markdown>{readable(assessment.diagnosis.reflection)}</Markdown>
      </details>
    </>
  );
}

export function assessmentReceipt(assessment: Assessment): string {
  if (assessment.assisted) return "辅助练习，未计入独立掌握证据";
  if (assessment.result === "ungraded") return "已保存回答，补充说明后继续评估";
  if (assessment.state_updates.length)
    return `已保存作答证据 · 更新 ${assessment.state_updates.length} 个知识点`;
  if (assessment.diagnosis.confidence < 0.8)
    return "已保存回答 · 诊断证据不足，掌握估计保持不变";
  return "已保存回答 · 同题练习未重复累计掌握证据";
}
