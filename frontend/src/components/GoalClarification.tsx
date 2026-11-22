import type { ClarificationDecision } from "../types";

interface Props {
  decision: ClarificationDecision;
  active: boolean;
  busy: boolean;
  onChoose: (option: string) => void;
}

export function GoalClarification({ decision, active, busy, onChoose }: Props) {
  if (!decision.options?.length) return null;
  return (
    <div className="clarification-options" aria-label="学习目标可选方向">
      {decision.options.map((option, index) => (
        <button
          key={option}
          type="button"
          disabled={busy || !active}
          onClick={() => onChoose(option)}
        >
          <span>{String.fromCharCode(65 + index)}</span>
          {option}
        </button>
      ))}
      {active && <small>也可以在下方输入你自己的想法。</small>}
    </div>
  );
}
