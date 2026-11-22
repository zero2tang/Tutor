"""生成核心设计学习文档的 SVG 与 PNG 图；所有图共享名称和配色。"""

from html import escape
from math import atan2, cos, hypot, pi, sin
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs/learning_harness/assets"
FONT = Path("C:/Windows/Fonts/msyh.ttc")
BOLD = Path("C:/Windows/Fonts/msyhbd.ttc")
COLORS = {
    "ink": "#183047",
    "muted": "#52687A",
    "line": "#8AA1B5",
    "blue": "#2874B2",
    "purple": "#8056AD",
    "green": "#238367",
    "orange": "#C9822C",
    "runtime": "#EFF6FD",
    "navy": "#193B59",
}


class Diagram:
    def __init__(self, name, width, height, title, subtitle):
        self.name, self.width, self.height = name, width, height
        self.image = Image.new("RGB", (width, height), "#FAFCFE")
        self.draw = ImageDraw.Draw(self.image)
        self.svg = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img"><title>{escape(title)}</title><desc>{escape(subtitle)}</desc><rect width="100%" height="100%" fill="#FAFCFE"/>'
        ]
        self.text(50, 34, title, 36, bold=True)
        self.text(52, 90, subtitle, 21, color=COLORS["muted"])

    def font(self, size, bold=False):
        return ImageFont.truetype(str(BOLD if bold and BOLD.exists() else FONT), size)

    def text(self, x, y, value, size=21, color=None, bold=False, center=False):
        color = color or COLORS["ink"]
        font = self.font(size, bold)
        if center:
            x -= self.draw.textlength(value, font=font) / 2
        self.draw.text((x, y), value, fill=color, font=font, anchor="lt")
        weight = "700" if bold else "400"
        self.svg.append(
            f'<text x="{x:.1f}" y="{y + size * .95:.1f}" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}">{escape(value)}</text>'
        )

    def rect(self, box, fill, stroke="#D4E0E9", radius=16, width=2):
        x1, y1, x2, y2 = box
        self.draw.rounded_rectangle(
            box, radius=radius, fill=fill, outline=stroke, width=width
        )
        self.svg.append(
            f'<rect x="{x1}" y="{y1}" width="{x2-x1}" height="{y2-y1}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>'
        )

    def wrap(self, value, max_width, size):
        rows, current = [], ""
        for char in value:
            if (
                current
                and self.draw.textlength(current + char, font=self.font(size))
                > max_width
            ):
                rows.append(current)
                current = char
            else:
                current += char
        return rows + ([current] if current else [])

    def box(
        self,
        box,
        title,
        lines=(),
        *,
        fill="#FFFFFF",
        stroke="#C9DAE8",
        color=None,
        size=20,
        title_size=25,
        gap=32,
    ):
        self.rect(box, fill, stroke)
        x1, y1, x2, y2 = box
        self.text(x1 + 22, y1 + 19, title, title_size, color=color, bold=True)
        y = y1 + (47 if y2 - y1 <= 120 else 61)
        for line in lines:
            for row in self.wrap(line, x2 - x1 - 44, size):
                if y + size > y2 - 13:
                    raise ValueError(
                        f"图中文字超出卡片：{self.name} / {title} / {line}"
                    )
                self.text(x1 + 22, y, row, size, color=color)
                y += gap

    def arrow(self, points, *, color=None, dashed=False, width=3):
        color = color or COLORS["blue"]
        if dashed:
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                distance = hypot(x2 - x1, y2 - y1)
                for start in range(0, int(distance), 15):
                    end = min(start + 8, distance)
                    self.draw.line(
                        (
                            x1 + (x2 - x1) * start / distance,
                            y1 + (y2 - y1) * start / distance,
                            x1 + (x2 - x1) * end / distance,
                            y1 + (y2 - y1) * end / distance,
                        ),
                        fill=color,
                        width=width,
                    )
        else:
            self.draw.line(points, fill=color, width=width, joint="curve")
        self.svg.append(
            f'<polyline points="{" ".join(f"{x},{y}" for x,y in points)}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round"{" stroke-dasharray=\"8 7\"" if dashed else ""}/>'
        )
        (x0, y0), (x, y) = points[-2:]
        angle = atan2(y - y0, x - x0)
        vertices = [
            (x, y),
            (x - 12 * cos(angle - pi / 6), y - 12 * sin(angle - pi / 6)),
            (x - 12 * cos(angle + pi / 6), y - 12 * sin(angle + pi / 6)),
        ]
        self.draw.polygon(vertices, fill=color)
        self.svg.append(
            f'<polygon points="{" ".join(f"{a:.1f},{b:.1f}" for a,b in vertices)}" fill="{color}"/>'
        )

    def label(self, x, y, value, color=None, size=18):
        width = self.draw.textlength(value, font=self.font(size))
        self.rect(
            (x - 5, y - 4, x + width + 5, y + size + 7), "#FAFCFE", "#FAFCFE", 4, 1
        )
        self.text(x, y, value, size, color=color or COLORS["muted"])

    def save(self):
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.image.save(OUTPUT / f"{self.name}.png")
        (OUTPUT / f"{self.name}.svg").write_text(
            "\n".join(self.svg) + "\n</svg>\n", encoding="utf-8"
        )


def overview():
    d = Diagram(
        "agent_loop_design_overview",
        2000,
        1680,
        "Tutor Harness 核心设计总览",
        "Agent loop 位于 Runtime 内部；外围提供策略、能力、业务状态与公开输出",
    )
    d.rect((520, 340, 1480, 1255), COLORS["runtime"], "#8EB6D6", 22, 3)
    d.text(550, 361, "Runtime 运行时：AgentRunner 管理一次 run", 25, bold=True)
    d.rect((570, 425, 1430, 1015), "#FFFFFF", "#A9C6DE", 18, 2)
    d.text(1020, 439, "中心：Agent loop", 24, bold=True)
    # 蓝色为请求与回执；绿色为业务提交；虚线表示投影或临时输出。
    d.arrow(
        [
            (550, 230),
            (500, 230),
            (500, 390),
            (620, 390),
            (620, 460),
            (700, 460),
            (700, 475),
        ]
    )
    d.arrow([(470, 490), (650, 490)], color=COLORS["purple"], dashed=True)
    d.arrow([(260, 685), (260, 640)], color=COLORS["purple"], dashed=True)
    d.arrow([(830, 475), (830, 410), (1510, 410), (1530, 410)])
    d.arrow([(1530, 505), (1380, 505)])
    d.arrow([(1225, 555), (1225, 665)])
    d.arrow([(1380, 690), (1530, 690)])
    d.arrow([(1530, 735), (1380, 735)])
    d.arrow([(1080, 715), (1000, 715)])
    d.arrow([(825, 665), (825, 555)])
    d.arrow([(1380, 535), (1405, 535), (1405, 895), (1200, 895)])
    d.arrow([(810, 905), (600, 905), (600, 515), (650, 515)], color=COLORS["orange"])
    d.arrow(
        [(1200, 925), (1454, 925), (1454, 1278), (1040, 1278), (1040, 1300)],
        color=COLORS["green"],
    )
    d.arrow(
        [(1530, 965), (1504, 965), (1504, 1400), (1480, 1400)], color=COLORS["green"]
    )
    d.arrow([(520, 1410), (495, 1410), (495, 975), (470, 975)], color=COLORS["green"])
    d.arrow([(1480, 1450), (1530, 1450)], color=COLORS["green"])
    d.arrow(
        [(1430, 980), (1490, 980), (1490, 1150), (1530, 1150)],
        color=COLORS["orange"],
        dashed=True,
    )
    d.arrow([(570, 1150), (470, 1150)], color=COLORS["line"], dashed=True)
    d.arrow([(1000, 1050), (1000, 1015)], color=COLORS["line"], dashed=True)
    d.box(
        (550, 160, 1450, 295),
        "业务用例控制",
        [
            "LearningService / LearningPlanner / GuidedTeachingService",
            "读取状态、选择流程、绑定可信能力；业务服务校验并提交",
        ],
        fill="#EAF6F0",
        stroke="#8EC3AE",
        gap=30,
    )
    d.box(
        (50, 350, 470, 640),
        "角色与可信输入",
        [
            "role / Skill / 输出 Schema",
            "TeachingRunPolicy 准备投影",
            "ToolContext：课程、学生、会话",
            "固定任务、目标与记忆来源",
            "正式评分隔离历史并关闭工具",
        ],
        fill="#F3EEF9",
        stroke="#C2ADDA",
    )
    d.box(
        (50, 685, 470, 1040),
        "业务状态与带来源的记忆",
        [
            "LearningSession / 目标图 / 进度",
            "冻结题目与提交前帮助快照",
            "Attempt / EvidenceConceptLink",
            "StudentState：有效证据的投影",
            "MemoryFact → 后台提取",
            "ScopeMemory → 后续教学上下文",
            "记忆读取遵守版本与撤销屏障",
        ],
        fill="#FFF7E9",
        stroke="#DAC39D",
    )
    d.box(
        (50, 1090, 470, 1500),
        "运行记录与业务恢复",
        [
            "AgentRun：执行轨迹及父子关系",
            "临时SSE：正文与运行进度",
            "SessionLease：本轮持有者",
            "幂等键与已完成委派产物回放",
            "context checkpoint：可重建摘要",
            "业务重放不等于恢复模型调用栈",
        ],
        fill="#F1F5F8",
        stroke="#B7C8D4",
    )
    d.box(
        (1530, 350, 1950, 565),
        "模型接口",
        [
            "ModelClient.chat（含流式回调）",
            "返回 tool_calls 或候选内容",
            "调用用量纳入本次运行记录",
        ],
        fill="#EEF4FC",
        stroke="#AFC5DE",
    )
    d.box(
        (1530, 610, 1950, 1015),
        "工具适配与业务能力",
        [
            "工具说明 / 参数 / 角色白名单",
            "ToolRegistry / AgentToolRegistry",
            "业务查询：课程、教材、RAG",
            "受限委派：知识 / 习题子 run",
            "子 run 使用独立 messages",
            "JSON 回执返回原父 loop",
            "由业务服务执行授权与事务",
            "查询通常只读；委派可保存题目",
        ],
        fill="#EBF7F4",
        stroke="#98C9BC",
        size=19,
        gap=34,
    )
    d.box(
        (1530, 1070, 1950, 1500),
        "公开输出与传输",
        [
            "公开字段 → text_delta 临时草稿",
            "草稿失败或取消时可撤回",
            "讲题正文经校验、提交后分段发布",
            "正式消息与持久事件支持恢复",
            "SSE 负责传输；正文分片不逐段入库",
            "事件记录与业务提交通常分开",
        ],
        fill="#FCEFF2",
        stroke="#D9AEBB",
        size=19,
        gap=37,
    )
    for box, title, line in [
        ((650, 475, 1000, 555), "① 上下文准备与模型请求", "每次请求前检查预算与来源"),
        ((1080, 475, 1380, 555), "② 处理模型响应", "工具调用或候选 JSON"),
        ((1080, 665, 1380, 765), "③ 调度工具", "并发、预算与错误回执"),
        ((650, 665, 1000, 765), "④ 追加工具回执", "按 tool_call_id 对应"),
        ((810, 855, 1200, 945), "⑤ 校验候选结果", "Schema 与可选业务校验"),
    ]:
        d.box(box, title, [line], title_size=21, size=17, gap=25)
    d.box(
        (570, 1050, 1430, 1220),
        "Runtime 运行基座",
        [
            "Invocation 与父子 run   ·   ContextController 与输入预算 / 来源检查",
            "工具轮次与调用数   ·   并发 / 熔断 / 循环超时 / 取消",
            "结构校验与修正反馈   ·   用量统计与注入的运行记录器",
        ],
        fill=COLORS["navy"],
        stroke=COLORS["navy"],
        color="#FFFFFF",
        size=20,
        gap=31,
    )
    d.box(
        (520, 1300, 1480, 1500),
        "业务服务校验与短事务提交",
        [
            "核对当前会话、执行权与版本，再提交相应业务事实。",
            "练习发布保存冻结契约；正式作答保存作答、证据、学生状态和记忆事实。",
            "提交可发生于委派工具内部，也可发生于父 run 返回后；不包住整次模型请求。",
        ],
        fill="#EAF6F0",
        stroke="#78B69B",
        size=20,
        gap=32,
    )
    for x, y, label, color in [
        (483, 462, "可信投影", COLORS["purple"]),
        (278, 650, "按范围读取", COLORS["purple"]),
        (1065, 385, "模型请求", None),
        (1390, 478, "模型响应", None),
        (1090, 600, "有工具调用", None),
        (1400, 660, "执行", None),
        (1400, 747, "JSON 回执", None),
        (670, 602, "继续原模型", None),
        (1250, 813, "无工具调用", None),
        (635, 807, "可修正错误 → 反馈重试", COLORS["orange"]),
        (1218, 1018, "合法结果交回业务用例", COLORS["green"]),
        (1545, 1027, "委派工具内部也可先提交", COLORS["green"]),
        (1020, 1021, "持续约束", COLORS["muted"]),
    ]:
        d.label(x, y, label, color, size=17)
    d.text(
        50,
        1542,
        "蓝色实线：请求与回执   绿色实线：业务提交   紫色虚线：可信投影   橙色虚线：临时草稿",
        21,
    )
    d.text(
        50,
        1585,
        "读图重点：工具结果回到原 loop；子 run 有独立上下文；业务服务拥有状态提交权。",
        22,
        bold=True,
    )
    d.text(
        50,
        1625,
        "这是主要逻辑关系图，省略部分函数调用；图中连线不表示源码直接 import。",
        19,
        color=COLORS["muted"],
    )
    d.save()


def loop_detail():
    d = Diagram(
        "agent_loop_execution_detail",
        1600,
        1530,
        "Agent loop 模块展开",
        "同一 run 内保持消息与回执配对；结束等待学生后，下一次请求会建立新的运行",
    )
    d.rect((50, 150, 1550, 1460), COLORS["runtime"], "#8EB6D6", 22, 3)
    d.arrow([(800, 260), (800, 320)])
    d.arrow([(800, 440), (800, 500)])
    d.arrow([(800, 610), (800, 685)])
    d.arrow([(560, 745), (320, 745), (320, 875)])
    d.arrow([(1040, 745), (1280, 745), (1280, 875)])
    d.arrow([(320, 1045), (320, 1110)])
    d.arrow([(100, 1170), (75, 1170), (75, 380), (560, 380)])
    d.arrow([(1280, 1045), (1280, 1110)], color=COLORS["green"])
    d.arrow(
        [(1500, 945), (1525, 945), (1525, 380), (1040, 380)], color=COLORS["orange"]
    )
    d.box(
        (560, 180, 1040, 260),
        "AgentRunner 创建本次运行",
        ["run_id / 独立 messages / 准备策略"],
        title_size=23,
        size=18,
    )
    d.box(
        (560, 320, 1040, 440),
        "每次请求前准备上下文",
        ["检查输入预算与记忆来源有效性", "必要时压缩完整、已闭合历史"],
        title_size=23,
        size=19,
        gap=28,
    )
    d.box(
        (560, 500, 1040, 610),
        "请求模型",
        ["允许的工具定义、消息与输出要求"],
        title_size=23,
        size=19,
    )
    d.box(
        (560, 685, 1040, 805),
        "处理模型响应",
        ["是否包含 tool_calls？"],
        title_size=23,
        size=20,
    )
    d.box(
        (100, 875, 540, 1045),
        "工具分支",
        [
            "先校验角色、参数、可信范围",
            "执行查询或受限业务委派",
            "受工具预算、并发与故障策略约束",
        ],
        title_size=24,
        size=19,
        gap=30,
    )
    d.box(
        (1060, 875, 1500, 1045),
        "候选结果分支",
        [
            "解析 JSON 并校验 Schema",
            "执行可选业务 validate_output",
            "可修正错误形成反馈，预算内重试",
        ],
        title_size=24,
        size=19,
        gap=30,
    )
    d.box(
        (100, 1110, 540, 1245),
        "把回执追加到原 messages",
        ["assistant 调用 + 对应 tool_call_id", "原调用方模型继续决策"],
        title_size=22,
        size=18,
        gap=28,
    )
    d.box(
        (1060, 1110, 1500, 1245),
        "返回结构化结果",
        ["交回业务用例继续校验或提交", "正式作答后的主 Agent 接续是新 run"],
        fill="#EAF6F0",
        stroke="#8EC3AE",
        title_size=23,
        size=18,
        gap=28,
    )
    d.box(
        (190, 1310, 1410, 1425),
        "Runtime 持续提供执行保障",
        [
            "轮次与工具预算 · 并发与熔断 · 取消与循环超时 · 用量与轨迹 · 未完成正文撤回",
            "租约丢失、取消及不可修正故障会结束当前运行；不作为普通工具输入错误无限重试。",
        ],
        fill=COLORS["navy"],
        stroke=COLORS["navy"],
        color="#FFFFFF",
        title_size=23,
        size=19,
        gap=28,
    )
    d.label(345, 760, "有工具调用")
    d.label(1080, 760, "无工具调用")
    d.label(110, 813, "回执进入下一轮输入", COLORS["blue"])
    d.label(1190, 349, "可修正错误 → 反馈", COLORS["orange"])
    d.text(
        60,
        1485,
        "委派工具可以先提交业务产物，再把回执交回父 loop；最终 JSON 并非所有业务副作用的起点。",
        20,
        color=COLORS["muted"],
    )
    d.save()


def state_detail():
    d = Diagram(
        "agent_state_evidence_memory",
        1800,
        1410,
        "业务状态、评分证据与派生记忆",
        "展开正式单题作答：进度、有效证据、掌握估计与学习记忆各有含义",
    )
    d.rect((580, 650, 1740, 1010), "#F0F9F4", "#8BC2A5", 22, 3)
    d.text(610, 666, "评分提交事务", 24, bold=True, color=COLORS["green"])
    d.arrow([(520, 235), (650, 235)])
    d.arrow([(290, 310), (290, 380)])
    d.arrow([(890, 310), (890, 400)])
    d.arrow([(890, 580), (890, 720)])
    d.arrow([(980, 815), (1040, 815)], color=COLORS["green"])
    d.arrow([(800, 720), (800, 705), (1555, 705), (1555, 720)], color=COLORS["green"])
    d.arrow([(1555, 910), (1555, 1080)], color=COLORS["purple"])
    d.arrow(
        [(1740, 1180), (1770, 1180), (1770, 235), (1740, 235)],
        color=COLORS["purple"],
        dashed=True,
    )
    d.arrow([(580, 945), (550, 945), (550, 960), (520, 960)], color=COLORS["green"])
    d.box(
        (80, 160, 520, 310),
        "学习会话与目标",
        ["LearningSession / SessionGoalState", "保存活动、目标、待答状态和版本"],
        fill="#EEF4FC",
        stroke="#AFC5DE",
        size=20,
        gap=31,
    )
    d.box(
        (80, 380, 520, 550),
        "流程进度",
        [
            "LearningContinuation / learning_progress",
            "例如：已讲解，下一步等待学生作答",
            "记录执行过什么，不直接修改掌握度",
        ],
        fill="#F1F5F8",
        stroke="#BACAD5",
        size=19,
        gap=31,
    )
    d.box(
        (650, 160, 1130, 310),
        "学生提交与帮助快照",
        ["当前原题、学生原答、提交前帮助条件", "用户消息可在正式评分事务之前保存"],
        fill="#F3EEF9",
        stroke="#C2ADDA",
        size=20,
        gap=31,
    )
    d.box(
        (650, 400, 1130, 580),
        "隔离评分与冻结评分包",
        [
            "规则判分 + assessment 角色",
            "仅按当前题、原答和帮助条件评分",
            "不注入历史画像、学习记忆或工具",
        ],
        fill="#EEF4FC",
        stroke="#AFC5DE",
        size=20,
        gap=32,
    )
    d.box(
        (620, 720, 980, 910),
        "作答与证据账本",
        [
            "Attempt / GradingPacket",
            "EvidenceConceptLink",
            "绑定原答片段、题目身份与来源版本",
        ],
        fill="#EAF6F0",
        stroke="#8EC3AE",
        size=19,
        title_size=23,
        gap=32,
    )
    d.box(
        (1040, 720, 1360, 910),
        "学生能力状态",
        ["StudentState", "学生域按有效账本归约", "重评可替换或撤回旧贡献"],
        fill="#EAF6F0",
        stroke="#8EC3AE",
        size=18,
        title_size=23,
        gap=32,
    )
    d.box(
        (1410, 720, 1700, 910),
        "原始记忆事实",
        ["MemoryFact", "同事务登记来源与版本", "不是模型生成的摘要"],
        fill="#EAF6F0",
        stroke="#8EC3AE",
        size=18,
        title_size=23,
        gap=32,
    )
    d.box(
        (80, 840, 520, 1050),
        "当前练习待答状态",
        [
            "发布时冻结题目、保留内部答案",
            "本次提交后按结果更新待答状态",
            "帮助以提交前记录为准",
            "答后补讲只影响后续作答",
        ],
        fill="#FFF7E9",
        stroke="#DAC39D",
        size=20,
        gap=32,
    )
    d.box(
        (1390, 1080, 1740, 1280),
        "后台派生学习记忆",
        [
            "MemoryWorker / 业务记忆任务",
            "提取候选，校验来源与版本",
            "ScopeMemory 等有来源记忆",
            "修订或遗忘可使旧来源失效",
        ],
        fill="#F3EEF9",
        stroke="#C2ADDA",
        size=18,
        title_size=23,
        gap=30,
    )
    d.box(
        (1370, 160, 1740, 310),
        "后续教学上下文",
        ["按课程、学生、概念范围投影", "请求前继续检查记忆撤销状态"],
        fill="#F3EEF9",
        stroke="#C2ADDA",
        size=19,
        title_size=23,
        gap=31,
    )
    d.text(
        620,
        947,
        "作答、证据、能力变化、记忆事实与相应待答状态更新共享原事务；中间失败一起回滚。",
        18,
        color=COLORS["green"],
    )
    d.label(690, 610, "冻结并验证局部证据")
    d.label(1425, 1022, "业务提交后另行处理", COLORS["purple"])
    d.text(80, 1130, "“讲过 / 做完步骤”不能直接推导“掌握”。", 24, bold=True)
    d.text(80, 1180, "正式评分后的反馈发布和主 Agent 接续发生在评分事务之后。", 21)
    d.text(80, 1220, "整次请求包含多个短事务，不是一个包住模型调用的大事务。", 21)
    d.text(
        80,
        1330,
        "读图重点：业务事实是依据；掌握度与记忆是不同投影；摘要、流程完成和模型观察都不能直接当成掌握证明。",
        20,
        color=COLORS["muted"],
    )
    d.save()


if __name__ == "__main__":
    overview()
    loop_detail()
    state_detail()
    print(f"Generated 3 SVG and 3 PNG files in {OUTPUT}")
