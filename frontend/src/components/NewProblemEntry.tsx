import { useState } from "react";
import { ArrowRight, ChevronDown, Plus } from "lucide-react";

export function NewProblemEntry({
  busy,
  blocked = false,
  defaultExpanded = false,
  onStart,
  onNewSession,
}: {
  busy: boolean;
  blocked?: boolean;
  defaultExpanded?: boolean;
  onStart: (stem: string) => void;
  onNewSession?: () => void;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [stem, setStem] = useState("");
  return (
    <section className="new-problem-entry" aria-label="讲解新习题">
      <button
        className="new-problem-toggle"
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <Plus size={18} />
        <span>
          <strong>讲解新习题</strong>
          <small>粘贴题干，从你的思路开始逐步讲解</small>
        </span>
        <ChevronDown size={17} className={expanded ? "rotated" : ""} />
      </button>
      {expanded &&
        (blocked ? (
          <div className="new-problem-body">
            <p role="status">当前会话已有练习。请新建会话后讲解另一道题。</p>
            {onNewSession && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={onNewSession}
              >
                新建会话
              </button>
            )}
          </div>
        ) : (
          <form
            className="new-problem-body"
            onSubmit={(event) => {
              event.preventDefault();
              if (!busy && stem.trim()) onStart(stem.trim());
            }}
          >
            <label htmlFor="new-problem-stem">新习题题干</label>
            <textarea
              id="new-problem-stem"
              rows={4}
              maxLength={8000}
              value={stem}
              disabled={busy}
              placeholder="粘贴完整题干、已知条件和问题，也可以写下你尝试过的思路。"
              onChange={(event) => setStem(event.target.value)}
            />
            <div className="new-problem-footer">
              <small>目前接受文字题干，缺少条件时会先向你确认。</small>
              <button className="primary" disabled={busy || !stem.trim()}>
                开始引导讲解
                <ArrowRight size={16} />
              </button>
            </div>
          </form>
        ))}
    </section>
  );
}
