import { useCallback, useEffect, useRef, useState } from "react";
import {
  BookOpen,
  ChartNoAxesCombined,
  ChevronDown,
  Compass,
  GitBranch,
  GraduationCap,
  History,
  Menu,
  Plus,
  Search,
  X,
} from "lucide-react";
import { api, streamOperation } from "./api";
import { updateDrafts, type StreamingDraft } from "./streaming/drafts";
import { observeSessionRecovery } from "./streaming/recovery";
import { Workspace, type SendOptions } from "./features/Workspace";
import type { BatchSubmission } from "./components/ExerciseBatchCard";
import { SearchPage } from "./features/SearchPage";
import { ConceptPage } from "./features/ConceptPage";
import { ProfilePage } from "./features/ProfilePage";
import { TextbooksPage } from "./features/TextbooksPage";
import {
  COURSE_STORAGE_KEY,
  DEFAULT_COURSE,
  SESSION_STORAGE_KEY,
  courseQuery,
  courseTitle,
} from "./courses";
import type {
  AgentRun,
  Concept,
  Course,
  ExerciseBatch,
  GraphData,
  Health,
  LearningSession,
  Message,
  Profile,
  Question,
  SessionSummary,
  View,
  GuidanceAction,
  GuidanceProblemInput,
  GuidanceState,
} from "./types";

const navigation = [
  { id: "textbooks" as View, label: "教材与方法", icon: BookOpen },
  { id: "workspace" as View, label: "学习空间", icon: Compass },
  { id: "concepts" as View, label: "知识地图", icon: GitBranch },
  { id: "search" as View, label: "习题检索", icon: Search },
  { id: "profile" as View, label: "学习档案", icon: ChartNoAxesCombined },
];
const activityNames: Record<string, string> = {
  supervisor: "主 Agent 正在推进本轮学习安排",
  knowledge: "正在整理知识与教学证据",
  exercise: "正在准备或校验习题",
  assessment: "正在独立评估学生状态",
};
const toolNames: Record<string, string> = {
  call_knowledge_agent: "读取知识点 Agent 返回的教学结果",
  call_exercise_agent: "读取习题 Agent 返回的练习或题解",
  get_difficulty_details: "查询题目难度与来源",
  search_by_difficulty: "按难度筛选和排序",
  read_skill: "读取所需的专业方法",
  get_course_overview: "了解当前课程与数据覆盖",
  search_concepts: "查找或浏览课程知识点",
  rank_learning_targets: "按指定标准比较学习目标",
  rag_retrieve: "检索课程文本与向量证据",
  graph_rag_retrieve: "结合知识关系检索材料",
  get_concept_details: "读取知识点详情",
  get_knowledge_structure: "读取知识结构",
  search_exercises: "检索真实练习",
  read_student_state: "读取已有学习证据",
  retrieve_learning_memory: "检索学习记忆",
  search_teaching_resources: "寻找相关例题与教材知识",
  read_teaching_resource: "阅读例题解析或教材原文",
};

interface RecoveryTarget {
  id: string;
  scope: string;
  courseVersion: number;
  sessionVersion: number;
  preserveError: boolean;
}

export default function App() {
  const [courseId, setCourseId] = useState(
    () => localStorage.getItem(COURSE_STORAGE_KEY) || DEFAULT_COURSE,
  );
  const currentCourse = useRef(courseId);
  const courseRequest = useRef(0);
  const sessionRequest = useRef(0);
  const selectingSession = useRef(false);
  const localStream = useRef<string | null>(null);
  const [streamingSessionId, setStreamingSessionId] = useState<string | null>(
    null,
  );
  const [recoveryTarget, setRecoveryTarget] = useState<RecoveryTarget | null>(
    null,
  );
  const [drafts, setDrafts] = useState<StreamingDraft[]>([]);
  const [courseLoading, setCourseLoading] = useState(true);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [view, setView] = useState<View>("workspace"),
    [mobileNav, setMobileNav] = useState(false);
  const [courses, setCourses] = useState<Course[]>([]),
    [concepts, setConcepts] = useState<Concept[]>([]);
  const [health, setHealth] = useState<Health | null>(null),
    [profile, setProfile] = useState<Profile | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]),
    [session, setSession] = useState<LearningSession | null>(null);
  const [graph, setGraph] = useState<GraphData | null>(null),
    [runs, setRuns] = useState<AgentRun[]>([]);
  const [busy, setBusy] = useState(false),
    [creating, setCreating] = useState(false),
    [activity, setActivity] = useState(""),
    [liveTools, setLiveTools] = useState<string[]>([]),
    [error, setError] = useState("");
  useEffect(() => {
    document.title =
      "理序 · " +
      courseTitle(
        courseId,
        courses.find((course) => course.id === courseId),
      );
  }, [courseId, courses]);
  const loadCourse = useCallback(
    async (nextCourse: string, restored: LearningSession | null = null) => {
      const request = ++courseRequest.current;
      ++sessionRequest.current;
      currentCourse.current = nextCourse;
      setCourseId(nextCourse);
      localStorage.setItem(COURSE_STORAGE_KEY, nextCourse);
      setCourseLoading(true);
      setSession(null);
      setRecoveryTarget(null);
      setDrafts([]);
      setRuns([]);
      setConcepts([]);
      setProfile(null);
      setSessions([]);
      setGraph(null);
      setLiveTools([]);
      setActivity("");
      setError("");
      const seeds = restored?.concept_ids.slice(0, 3) || [];
      const graphPath = seeds.length
        ? `/graph?concept_ids=${encodeURIComponent(seeds.join(","))}`
        : "/graph";
      try {
        const [conceptData, profileData, history, graphData, runData] =
          await Promise.all([
            api<{ items: Concept[] }>(courseQuery("/concepts", nextCourse)),
            api<Profile>(courseQuery("/student", nextCourse)),
            api<{ items: SessionSummary[] }>(
              courseQuery("/sessions", nextCourse),
            ),
            api<GraphData>(courseQuery(graphPath, nextCourse)),
            restored
              ? api<{ items: AgentRun[] }>(`/sessions/${restored.id}/runs`)
              : Promise.resolve({ items: [] as AgentRun[] }),
          ]);
        // 课程请求有版本号，慢返回的上一门课不能覆盖当前课程。
        if (request !== courseRequest.current) return;
        setConcepts(conceptData.items);
        setProfile(profileData);
        setSessions(history.items);
        setGraph(graphData);
        setRuns(runData.items);
        setSession(restored);
        setBusy(Boolean(restored?.active_turn_id));
        if (restored) localStorage.setItem(SESSION_STORAGE_KEY, restored.id);
      } catch (reason) {
        if (request === courseRequest.current)
          setError(
            reason instanceof Error ? reason.message : "课程资料加载失败",
          );
      } finally {
        if (request === courseRequest.current) setCourseLoading(false);
      }
    },
    [],
  );
  const refreshProfile = useCallback(async (scope = currentCourse.current) => {
    const request = courseRequest.current;
    const sessionVersion = sessionRequest.current;
    const data = await api<Profile>(courseQuery("/student", scope));
    if (
      request === courseRequest.current &&
      sessionVersion === sessionRequest.current &&
      scope === currentCourse.current
    )
      setProfile(data);
  }, []);
  const refreshSession = useCallback(
    async (id: string, scope = currentCourse.current) => {
      const request = ++sessionRequest.current;
      const courseVersion = courseRequest.current;
      const [data, runData] = await Promise.all([
        api<LearningSession>(`/sessions/${id}`),
        api<{ items: AgentRun[] }>(`/sessions/${id}/runs`),
      ]);
      if (
        request !== sessionRequest.current ||
        courseVersion !== courseRequest.current
      )
        return;
      const sessionCourse = data.course_id || DEFAULT_COURSE;
      if (sessionCourse !== scope) {
        await loadCourse(sessionCourse, data);
        return;
      }
      const graphPath = data.concept_ids.length
        ? `/graph?concept_ids=${encodeURIComponent(data.concept_ids.slice(0, 3).join(","))}`
        : "/graph";
      const [history, graphData] = await Promise.all([
        api<{ items: SessionSummary[] }>(
          courseQuery("/sessions", sessionCourse),
        ),
        api<GraphData>(courseQuery(graphPath, sessionCourse)),
      ]);
      if (
        request !== sessionRequest.current ||
        courseVersion !== courseRequest.current
      )
        return;
      setSession(data);
      setRuns(runData.items);
      setSessions(history.items);
      setGraph(graphData);
      localStorage.setItem(SESSION_STORAGE_KEY, id);
      return data;
    },
    [loadCourse],
  );
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [courseData, healthData] = await Promise.all([
          api<{ items: Course[] }>("/courses"),
          api<Health>("/health"),
        ]);
        if (!alive) return;
        setCourses(courseData.items);
        setHealth(healthData);
        const saved = localStorage.getItem(SESSION_STORAGE_KEY);
        let restored: LearningSession | null = null;
        if (saved) {
          restored = await api<LearningSession>(`/sessions/${saved}`).catch(
            () => {
              localStorage.removeItem(SESSION_STORAGE_KEY);
              return null;
            },
          );
        }
        if (!alive) return;
        // 旧版会话没有 course_id，按力学恢复；服务端返回的课程优先。
        const requested = restored
          ? restored.course_id || DEFAULT_COURSE
          : currentCourse.current;
        const available = courseData.items.some(
          (course) => course.id === requested,
        );
        await loadCourse(
          available ? requested : DEFAULT_COURSE,
          available ? restored : null,
        );
      } catch (reason) {
        if (alive) {
          setError(reason instanceof Error ? reason.message : "后端连接失败");
          setCourseLoading(false);
        }
      }
    })();
    return () => {
      alive = false;
      ++courseRequest.current;
    };
  }, [loadCourse]);

  useEffect(() => {
    const target =
      recoveryTarget ||
      (session?.active_turn_id
        ? {
            id: session.id,
            scope: session.course_id || DEFAULT_COURSE,
            courseVersion: courseRequest.current,
            sessionVersion: sessionRequest.current,
            preserveError: false,
          }
        : null);
    if (!target || streamingSessionId === target.id) return;
    const { id, scope, courseVersion, sessionVersion } = target;
    const controller = new AbortController();
    const isCurrent = () =>
      courseVersion === courseRequest.current &&
      sessionVersion === sessionRequest.current &&
      scope === currentCourse.current &&
      localStream.current !== id;
    if (!isCurrent()) return;
    setBusy(true);
    setActivity("正在恢复尚未结束的学习任务");
    // 断流后的恢复目标独立于旧快照，旧 active_turn_id 为空也会自动重试。
    void observeSessionRecovery({
      signal: controller.signal,
      isCurrent,
      read: async (signal) => {
        const current = await api<LearningSession>(`/sessions/${id}`, {
          signal,
        });
        if (
          current.id !== id ||
          (current.course_id || DEFAULT_COURSE) !== scope
        )
          throw new Error("恢复快照与当前会话不匹配");
        return current;
      },
      onSnapshot: (current) => {
        setSession(current);
        if (!current.active_turn_id) {
          setRecoveryTarget(null);
          setBusy(false);
          setActivity("");
          if (current.phase === "interrupted")
            setError("上一轮任务未完成，可以在当前会话继续提问或重试。");
          else if (!target.preserveError) setError("");
        }
      },
      refreshDetails: () =>
        Promise.allSettled([refreshSession(id, scope), refreshProfile(scope)]),
    });
    return () => controller.abort();
  }, [
    session?.id,
    session?.active_turn_id,
    streamingSessionId,
    recoveryTarget,
    refreshSession,
    refreshProfile,
  ]);

  const perform = async (id: string, path: string, body: unknown) => {
    if (
      busy ||
      courseLoading ||
      selectingSession.current ||
      localStream.current
    )
      return;
    const scope = currentCourse.current;
    const courseVersion = courseRequest.current;
    // 新一轮使上一轮辅助资料的慢返回失效，不能覆盖已经收到的新消息。
    const sessionVersion = ++sessionRequest.current;
    const isCurrent = () =>
      courseVersion === courseRequest.current && localStream.current === id;
    let preserveError = false;
    localStream.current = id;
    setStreamingSessionId(id);
    setDrafts([]);
    setBusy(true);
    setError("");
    setLiveTools([]);
    setActivity("正在启动本轮学习");
    try {
      await streamOperation(`/sessions/${id}/${path}`, body, (event, data) => {
        if (!isCurrent()) return;
        const value = data as Record<string, unknown>;
        if (event === "error") preserveError = true;
        setDrafts((previous) => updateDrafts(previous, event, data));
        if (event === "agent_started")
          setActivity(activityNames[String(value.agent)] || "正在处理");
        if (event === "agent_returned")
          setActivity(
            value.caller === "knowledge"
              ? "习题结果已返回，正在整合并交回主 Agent"
              : "子 Agent 已返回，主 Agent 正在接续决策",
          );
        if (event === "tool_completed")
          setLiveTools((previous) =>
            [
              ...previous,
              toolNames[String(value.name)] || String(value.name),
            ].slice(-8),
          );
        if (event === "message")
          setSession((previous) =>
            previous?.id === id
              ? {
                  ...previous,
                  messages: previous.messages.some(
                    (m) => m.id === (data as Message).id,
                  )
                    ? previous.messages
                    : [...previous.messages, data as Message],
                }
              : previous,
          );
        if (event === "guidance")
          setSession((previous) =>
            previous?.id === id
              ? { ...previous, guidance: data as GuidanceState }
              : previous,
          );
        if (event === "question")
          setSession((previous) =>
            previous?.id === id
              ? {
                  ...previous,
                  pending_question: data as Question,
                  phase: "awaiting_answer",
                }
              : previous,
          );
        if (event === "exercise_batch")
          setSession((previous) =>
            previous?.id === id
              ? {
                  ...previous,
                  pending_batch: data as ExerciseBatch,
                  pending_question: null,
                  phase: "awaiting_answer",
                }
              : previous,
          );
        if (event === "batch_generation_started")
          setActivity(`正在并行生成 ${value.count} 道练习`);
        if (event === "batch_item_completed")
          setLiveTools((previous) =>
            [...previous, `第 ${value.index} 题已通过校验`].slice(-8),
          );
        if (event === "batch_assessment_completed")
          setSession((previous) =>
            previous?.id === id
              ? {
                  ...previous,
                  pending_batch: null,
                  phase: "learning",
                }
              : previous,
          );
        if (event === "plan")
          setSession((previous) =>
            previous?.id === id
              ? {
                  ...previous,
                  agenda: value.steps as string[],
                  concept_ids: value.concept_ids as string[],
                }
              : previous,
          );
      });
    } catch (reason) {
      if (isCurrent()) {
        preserveError = true;
        setError(
          reason instanceof Error ? reason.message : "任务暂未完成，请重试",
        );
      }
    } finally {
      // 收尾先进入独立恢复状态；网络故障不能被当作后端任务已经结束。
      if (isCurrent()) {
        setDrafts([]);
        setRecoveryTarget({
          id,
          scope,
          courseVersion,
          sessionVersion,
          preserveError,
        });
        setStreamingSessionId(null);
        setBusy(true);
        setActivity("正在同步学习记录");
      }
      if (localStream.current === id) localStream.current = null;
    }
  };
  const openSession = async (id: string) => {
    if (
      busy ||
      creating ||
      courseLoading ||
      selectingSession.current ||
      localStream.current
    )
      return;
    // 同步加锁，避免点击历史后、React 更新前把新消息发给旧会话。
    selectingSession.current = true;
    setSessionLoading(true);
    setRecoveryTarget(null);
    setDrafts([]);
    setLiveTools([]);
    setActivity("");
    setError("");
    setView("workspace");
    setMobileNav(false);
    try {
      const restored = await refreshSession(id);
      if (restored) setBusy(Boolean(restored.active_turn_id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "学习记录加载失败");
    } finally {
      selectingSession.current = false;
      setSessionLoading(false);
    }
  };
  const start = async (goal: string, ids: string[]) => {
    if (busy || courseLoading || selectingSession.current || creating) return;
    const scope = currentCourse.current;
    setCreating(true);
    setError("");
    setView("workspace");
    try {
      const created = await api<LearningSession>("/sessions", {
        method: "POST",
        body: JSON.stringify({ goal, concept_ids: ids, course_id: scope }),
      });
      if (scope !== currentCourse.current) return;
      setSession(created);
      localStorage.setItem(SESSION_STORAGE_KEY, created.id);
      await perform(created.id, "messages", {
        content: goal,
        intent: "auto",
        concept_ids: ids,
        request_id: crypto.randomUUID(),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法开始学习");
    } finally {
      setCreating(false);
    }
  };
  const send = (content: string, options: SendOptions = {}) => {
    if (session)
      void perform(session.id, "messages", {
        content,
        ...options,
        request_id: crypto.randomUUID(),
        ...(session.pending_interaction ? { wait_id: session.pending_interaction.wait_id } : {}),
      });
  };
  const guide = async (
    action: GuidanceAction,
    text = "",
    problem?: GuidanceProblemInput,
  ) => {
    if (busy || courseLoading || selectingSession.current || creating) return;
    const scope = currentCourse.current;
    setError("");
    setView("workspace");
    setCreating(true);
    try {
      let target = session;
      if (!target) {
        target = await api<LearningSession>("/sessions", {
          method: "POST",
          body: JSON.stringify({
            goal: "围绕新习题进行引导式学习",
            concept_ids: [],
            course_id: scope,
          }),
        });
        if (scope !== currentCourse.current) return;
        setSession(target);
        localStorage.setItem(SESSION_STORAGE_KEY, target.id);
      }
      await perform(target.id, "guidance", {
        action,
        text,
        ...(problem ? { problem } : {}),
        request_id: crypto.randomUUID(),
        expected_version: target.guidance?.version ?? 0,
        ...(target.pending_interaction ? { wait_id: target.pending_interaction.wait_id } : {}),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法开始引导讲解");
    } finally {
      setCreating(false);
    }
  };
  const answer = (value: string) => {
    if (!session) return;
    const pending = session.pending_interaction;
    if (!pending?.wait_id || !pending.question_id) {
      setError("当前作答停点尚未同步，请刷新学习记录后重试。");
      return;
    }
    void perform(session.id, "answers", {
      answer: value,
      request_id: crypto.randomUUID(),
      wait_id: pending.wait_id,
      question_id: pending.question_id,
    });
  };
  const answerBatch = (submission: BatchSubmission) => {
    if (!session) return;
    if (!session.pending_interaction?.wait_id) {
      setError("当前题组停点尚未同步，请刷新学习记录后重试。");
      return;
    }
    void perform(session.id, "batch-answers", {
      ...submission,
      wait_id: session.pending_interaction.wait_id,
    });
  };
  const practice = async (question: Question) => {
    if (busy || courseLoading || selectingSession.current || creating) return;
    const questionCourse = question.course_id || DEFAULT_COURSE;
    if (questionCourse !== currentCourse.current) {
      setError("这道题属于另一门课程，请切换课程后再练习。");
      return;
    }
    setView("workspace");
    setError("");
    setCreating(true);
    try {
      const created = await api<LearningSession>("/sessions", {
        method: "POST",
        body: JSON.stringify({
          goal: "通过练习掌握：" + question.stem.slice(0, 60),
          concept_ids: question.concept_ids,
          course_id: questionCourse,
        }),
      });
      setSession(created);
      localStorage.setItem(SESSION_STORAGE_KEY, created.id);
      await perform(created.id, "practice", {
        question_id: question.id,
        cognitive_type: "understanding",
        request_id: crypto.randomUUID(),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法创建练习");
    } finally {
      setCreating(false);
    }
  };
  const reset = () => {
    if (busy || courseLoading || selectingSession.current || creating) return;
    ++sessionRequest.current;
    setSession(null);
    setRecoveryTarget(null);
    setDrafts([]);
    setLiveTools([]);
    setActivity("");
    setError("");
    setRuns([]);
    localStorage.removeItem(SESSION_STORAGE_KEY);
    setView("workspace");
    setMobileNav(false);
    const scope = currentCourse.current;
    void api<GraphData>(courseQuery("/graph", scope))
      .then((data) => {
        if (scope === currentCourse.current) setGraph(data);
      })
      .catch((reason) => setError(String(reason)));
  };
  const changeCourse = (nextCourse: string) => {
    if (
      busy ||
      creating ||
      selectingSession.current ||
      nextCourse === currentCourse.current
    )
      return;
    localStorage.removeItem(SESSION_STORAGE_KEY);
    setView("workspace");
    setMobileNav(false);
    void loadCourse(nextCourse);
  };
  const activeCourse = courses.find((course) => course.id === courseId);
  const activeCourseTitle = courseTitle(courseId, activeCourse);
  const unavailable = busy || creating || courseLoading || sessionLoading;
  const title = navigation.find((item) => item.id === view)?.label;
  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">理</div>
          <div>
            <strong>理序</strong>
            <span>LEARN WITH REASON</span>
          </div>
        </div>
        <div className="course-switch">
          <div className="course-icon">
            <GraduationCap size={20} />
          </div>
          <label className="course-picker">
            <small>当前课程</small>
            <select
              aria-label="选择课程"
              value={courseId}
              disabled={unavailable}
              onChange={(event) => changeCourse(event.target.value)}
            >
              {!courses.length && (
                <option value={courseId}>{activeCourseTitle}</option>
              )}
              {courses.map((course) => {
                const status = course.index_status;
                const building =
                  status &&
                  (!["ready", "not_required"].includes(status.es) ||
                    !["ready", "not_required"].includes(status.neo4j));
                return (
                  <option
                    key={course.id}
                    value={course.id}
                    disabled={Boolean(building)}
                  >
                    {courseTitle(course.id, course)}
                    {building ? "（构建中）" : ""}
                  </option>
                );
              })}
            </select>
          </label>
          <ChevronDown size={15} aria-hidden="true" />
        </div>
        <nav className="navigation">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={view === item.id ? "active" : ""}
              onClick={() => {
                setView(item.id);
                setMobileNav(false);
              }}
            >
              <item.icon size={19} />
              {item.label}
              {view === item.id && <span />}
            </button>
          ))}
        </nav>
        <button className="new-session" disabled={unavailable} onClick={reset}>
          <Plus size={17} />
          开始新的学习
        </button>
        <div className="history-heading">
          <History size={14} />
          <span>最近学习</span>
        </div>
        <div className="session-history">
          {sessions.slice(0, 6).map((item) => (
            <button
              disabled={unavailable}
              key={item.id}
              className={session?.id === item.id ? "selected" : ""}
              onClick={() => void openSession(item.id)}
            >
              <span>{item.title}</span>
              <small>
                {new Date(item.updated_at + "Z").toLocaleDateString("zh-CN", {
                  month: "numeric",
                  day: "numeric",
                })}
              </small>
            </button>
          ))}
          {!sessions.length && <p>你的学习记录会保存在这里</p>}
        </div>
        <div className="sidebar-bottom">
          <div className="local-avatar">学</div>
          <div>
            <strong>本机学习档案</strong>
            <span>学习记录已持久保存</span>
          </div>
          <span className="status-dot" />
        </div>
      </aside>
      {mobileNav && (
        <button
          className="nav-backdrop"
          aria-label="关闭导航"
          onClick={() => setMobileNav(false)}
        />
      )}
      <main className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              onClick={() => setMobileNav(true)}
            >
              <Menu size={20} />
            </button>
            <BookOpen size={16} />
            <span>{activeCourseTitle}</span>
            <b>/</b>
            <strong>{title}</strong>
          </div>
          <div className="topbar-right">
            <span
              className={`connection-badge ${health?.status === "ready" ? "" : "offline"}`}
            >
              <i />
              {health?.status === "ready" ? "学习服务已连接" : "正在连接服务"}
            </span>
            <span className="edition">个人学习版</span>
          </div>
        </header>
        {error && (
          <div className="error-banner global-error">
            <span>{error}</span>
            <button
              className="icon-button"
              aria-label="关闭提示"
              onClick={() => setError("")}
            >
              <X size={16} />
            </button>
          </div>
        )}
        {view === "workspace" && (
          <Workspace
            key={courseId}
            courseId={courseId}
            courseTitle={activeCourseTitle}
            session={session}
            concepts={concepts}
            profile={profile}
            graph={graph}
            runs={runs}
            busy={unavailable}
            activity={activity}
            liveTools={liveTools}
            drafts={drafts}
            onStart={(goal, ids) => void start(goal, ids)}
            onSend={send}
            onGuidance={(action, text, problem) =>
              void guide(action, text, problem)
            }
            onNewSession={reset}
            onAnswer={answer}
            onBatchAnswer={answerBatch}
            onSkip={() => {
              if (session) {
                if (!session.pending_interaction?.wait_id) {
                  setError("当前等待停点尚未同步，请刷新学习记录后重试。");
                  return;
                }
                void api(`/sessions/${session.id}/skip`, {
                  method: "POST",
                  body: JSON.stringify({
                    request_id: crypto.randomUUID(),
                    wait_id: session.pending_interaction.wait_id,
                  }),
                })
                  .then(() => refreshSession(session.id))
                  .catch((reason) => setError(String(reason)));
              }
            }}
            onSelectConcept={(id) => {
              const concept = concepts.find((c) => c.id === id);
              if (concept && !unavailable)
                void start("学习" + concept.name, [id]);
            }}
          />
        )}
        {view === "search" && (
          <SearchPage
            key={courseId}
            courseId={courseId}
            concepts={concepts}
            onPractice={(question) => void practice(question)}
          />
        )}
        {view === "textbooks" && (
          <TextbooksPage
            key={courseId}
            courseId={courseId}
            busy={unavailable}
            onPractice={(question) => void practice(question)}
          />
        )}
        {view === "concepts" && (
          <ConceptPage
            key={courseId}
            courseId={courseId}
            concepts={concepts}
            onLearn={(concept) =>
              void start("学习" + concept.name, [concept.id])
            }
          />
        )}
        {view === "profile" && (
          <ProfilePage
            profile={profile}
            onRefresh={() => void refreshProfile()}
            onForget={(id) =>
              void api(`/student/memories/${id}`, { method: "DELETE" }).then(
                () => refreshProfile(),
              )
            }
          />
        )}
        <footer className="app-footer">
          <span>理序 · 以证据理解学习</span>
          <span>
            {courseLoading ? (
              "正在加载课程…"
            ) : (
              <>
                {activeCourse?.question_count ?? "—"} 道课程题目 ·{" "}
                {activeCourse?.concept_count ?? "—"} 个
                {courseId === "math" ? "知识节点" : "知识点"}
              </>
            )}
          </span>
        </footer>
      </main>
    </div>
  );
}
