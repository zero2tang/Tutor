import type { BatchAssessment, Concept } from "../types";
import { AssessmentMessage, assessmentReceipt } from "./AssessmentMessage";
import { Markdown } from "./Markdown";

export function BatchAssessmentMessage({
  assessment,
  concepts,
}: {
  assessment: BatchAssessment;
  concepts: Concept[];
}) {
  return (
    <section className="batch-assessment">
      <h3>整组学习反馈</h3>
      <p>
        共 {assessment.total} 题 · 正确 {assessment.correct_count} 题 · 已判分{" "}
        {assessment.graded_count} 题
      </p>
      <Markdown>{assessment.summary}</Markdown>
      {assessment.items.map((item, index) => (
        <div className="batch-item-feedback" key={item.attempt_id}>
          <strong>第 {index + 1} 题</strong>
          <AssessmentMessage assessment={item} content="" concepts={concepts} />
          <p className="context-note">{assessmentReceipt(item)}</p>
        </div>
      ))}
    </section>
  );
}
