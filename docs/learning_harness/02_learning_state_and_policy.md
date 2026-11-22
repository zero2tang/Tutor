# 02 · 流程、原答、证据与掌握状态

当前框架迁移保留领域证据规则。本篇以[评分包](../../backend/app/domains/assessment/evidence.py)和[掌握度归约](../../backend/app/domains/students/mastery.py)为准；这些既有规则不重复记为本次框架新增能力。

## 1. 四种状态不能相互替代

| 状态 | 保存位置 | 可以证明什么 |
|---|---|---|
| 学习流程进度 | LearningSession、learning_progress、主子目标 | 教学任务到了哪一步，接下来等谁 |
| 学生原答与评估 | Message、Attempt／BatchSubmission | 学生本次说了什么，针对哪份冻结题进行评价 |
| 掌握证据与状态 | EvidenceConceptLink、StudentState | 哪个知识点、哪个维度有直接且合格的独立证据 |
| 语义教学记忆 | 观察、作用域记忆、错因记录 | 后续教学应参考哪些有来源的观察和偏好 |

讲过、看过答案、流程 completed 都不能直接成为独立作答证据。数值未知用 None 表示，不等于零分。

## 2. pending_context 保存正式交互

当前单题 contract、assisted／帮助快照、exercise_batch、guided_teaching、澄清 decision 和 learning_progress 属于不同生命周期。[preserve_learning_context](../../backend/app/domains/sessions/progress.py)只深拷贝正式 learning_progress，不再延续 delegation_checkpoint。

切换题目可以显式保留最近讨论题或工作区，但不能靠聊天摘要重建标准答案。历史私有契约保存在精确出题消息中，不能因题库内容后来修改而换用另一版本答案。

## 3. 从原答到证据

会话图先保存昂贵评分候选，再由 commit 在短事务内核对 owner、当前冻结题、原答指纹和提交前帮助条件。评分包冻结本次评价输入，证据链接按知识点保存明确来源和局部结果；综合题没有局部依据的知识点保持 not_observed。

必要准入条件可概括为：

\[
eligible=\neg assisted \land result\neq ungraded
\land confidence_{\mathrm{overall}}\ge 0.8
\land attribution=direct
\land confidence_{\mathrm{local}}\ge 0.8
\land valid\_local\_score.
\]

具体条件以 freeze_grading_packet 为准。程序可确定的选择题判分优先于模型总体结论；填空数学等价和复杂开放作答仍依赖模型及业务约束。

## 4. 掌握度怎样归约

正式账本按稳定 canonical_item_id 与 dimension 去重，重评替换旧贡献，不直接叠加诊断模型后续建议。当前 apply_ledger_evidence 对一个维度先按结果初始化：

\[
m_1 =
\begin{cases}
0.5 & correct\\
0.25 & partial\\
0 & incorrect
\end{cases}
\]

后续合格独立证据采用固定增量：答对 +0.15，答错 −0.15，部分正确 0；先限制到 [0,1]，再按该维度证据数量设置上限：

\[
u(n)=
\begin{cases}
0.5 & n=1\\
0.8 & n=2\\
1 & n\ge3
\end{cases},
\qquad m'=\operatorname{round}(\min(u(n),\operatorname{clip}(m+\Delta,0,1)),3).
\]

这是明确的启发式规则，不是 BKT、IRT 或经校准的答对概率。旧 baseline 的纳入有专门处理；不能把旧 StudentService 中另一条历史更新函数的 suggested_delta 公式当成当前账本主链。

## 5. 目标和学习进度

[goals.py](../../backend/app/domains/sessions/goals.py)拥有本会话主子目标、活动路径和补救返回栈，使用版本校验。更换根目标需要当前学生明确原话，内部评估后接续不能擅自改目标。

[save_progress](../../backend/app/domains/learning/progress.py)合并当前同一目标的执行步骤，去重并保留最近 24 项；改变目标不把旧步骤自动计入新目标。它不写掌握度。

验证可先读[证据测试](../../backend/tests/test_concept_evidence.py)、[进度测试](../../backend/tests/test_learning_progress.py)、[冻结上下文测试](../../backend/tests/test_exercise_context.py)。Attempt 已存在而 state_updates 为空，是允许的业务结果。
