// 真实教材页面→通用引导讲解→自由追问→刷新恢复；学生发言为明确的验收模拟。
// 依赖已运行的 8008 后端、5173 前端、已导入的教材，以及已安装的 Playwright / Edge。
// TUTOR_BROWSER_MODULES 指向现有 node_modules；不安装新的浏览器或依赖。
const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "../..");
const apiBase = (
  process.env.TUTOR_API_BASE_URL || "http://127.0.0.1:8008/api"
).replace(/\/$/, "");
const frontendUrl = process.env.TUTOR_FRONTEND_URL || "http://127.0.0.1:5173";
const catalogOnly = process.argv.includes("--catalog-only");
const reportPath = path.join(root, "data/reports/textbook_ui_acceptance.json");

async function api(resource) {
  const response = await fetch(apiBase + resource);
  if (!response.ok) {
    throw new Error(
      `${resource}: HTTP ${response.status} ${(await response.text()).slice(0, 300)}`,
    );
  }
  return response.json();
}

async function selectTextbook() {
  // 从当前 API 读取来源目录，不依赖旧验收报告、数学答案或 ready 数量。
  const documents =
    (await api("/textbooks?course_id=junior_algebra")).items || [];
  const candidates = [];
  for (const document of documents) {
    if (!document.exercise_count) continue;
    const packs =
      (
        await api(
          `/teaching-packs?course_id=junior_algebra&document_id=${document.id}&limit=100`,
        )
      ).items || [];
    packs.forEach((pack, index) => {
      if (
        !pack.question_id ||
        !pack.problem?.stem?.trim() ||
        !pack.source_refs?.length
      )
        return;
      const sourceCatalog =
        pack.provenance?.analysis_version === "textbook-catalog-v1";
      const officialExcerpt = ["curated_excerpt", "open_textbook"].includes(
        document.source_kind,
      );
      candidates.push({
        document,
        pack,
        index,
        score:
          Number(officialExcerpt) * 16 +
          Number(sourceCatalog) * 8 +
          Number(pack.status === "reference_only") * 2,
      });
    });
  }
  candidates.sort((a, b) => b.score - a.score);
  assert(
    candidates.length,
    "请先通过 /api/textbooks/import 导入含题干和来源定位的教材；不要求原答案或数学核验状态。",
  );
  return candidates[0];
}

async function main() {
  if (process.argv.includes("--help")) {
    console.log(
      "教材→通用引导 UI 验收。默认会以 evaluation 身份点击真实教材题并发起一次自由追问，调用配置中的真实模型。\n--catalog-only 仅检查课程、教材和启动按钮，不发起模型请求，报告明确标为部分检查。\n需要现有 Playwright/Edge：设置 TUTOR_BROWSER_MODULES 为已安装的 node_modules；可用 TUTOR_API_BASE_URL、TUTOR_FRONTEND_URL 覆盖服务地址。",
    );
    return;
  }
  const { chromium } = require(
    process.env.TUTOR_BROWSER_MODULES
      ? path.join(process.env.TUTOR_BROWSER_MODULES, "playwright")
      : "playwright",
  );
  const target = await selectTextbook();
  const student =
    "evaluation_" + crypto.randomUUID().replaceAll("-", "").slice(0, 12);
  const report = {
    started_at: new Date().toISOString(),
    complete: false,
    mode: catalogOnly ? "catalog_only" : "real_guided_teaching",
    student_id: student,
    document_id: target.document.id,
    question_id: target.pack.question_id,
    source_status: target.pack.status,
    source_analysis_version: target.pack.provenance?.analysis_version || null,
    data_origin: "real_textbook_and_explicit_simulated_browser_followup",
    report_dependency: "none; source selected through current backend API",
    checks: [],
    page_errors: [],
    requests: [],
    not_tested: ["真实学生学习效果", "模型讲解数学正确率", "图片识别质量"],
  };
  await fs.mkdir(path.dirname(reportPath), { recursive: true });
  const browser = await chromium.launch({
    channel: process.env.TUTOR_BROWSER_CHANNEL || "msedge",
    headless: true,
  });
  let page, sid;
  try {
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(240000);
    page.on("pageerror", (error) => report.page_errors.push(error.message));
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (request.method() === "POST" && /\/api\/sessions/.test(url.pathname)) {
        report.requests.push({ method: request.method(), path: url.pathname });
      }
    });
    // 只覆盖创建请求中的学生身份，不篡改模型结果、工作区状态或后端回执。
    await page.route("**/api/sessions", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const body = route.request().postDataJSON();
      return route.continue({
        postData: JSON.stringify({ ...body, student_id: student }),
      });
    });
    await page.goto(frontendUrl);
    await page.getByLabel("选择课程").selectOption("junior_algebra");
    await page.waitForFunction(
      () => localStorage.getItem("stem-tutor-course") === "junior_algebra",
    );
    await page.getByRole("button", { name: "教材与方法", exact: true }).click();
    await page.getByLabel("选择教材").selectOption(target.document.id);
    const card = page
      .locator(".textbook-pack-list .textbook-card")
      .nth(target.index);
    const launch = card.getByRole("button", {
      name: "开始引导讲解",
      exact: true,
    });
    await launch.waitFor();
    assert(
      await launch.isEnabled(),
      `结构有效的教材题应能开始讲解，当前目录状态为 ${target.pack.status}`,
    );
    assert((await card.textContent()).trim(), "教材题干没有显示");
    report.checks.push(
      "课程切换至初中课程，教材原文目录和引导入口可读取；不要求 ready 数量或预存答案",
    );
    await page.screenshot({
      path: path.join(root, "data/reports/textbook_library_ui.png"),
      fullPage: false,
    });
    if (!catalogOnly) {
      const created = page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === "/api/sessions" &&
          response.request().method() === "POST",
      );
      await launch.click();
      const creation = await created;
      assert(creation.ok(), `创建 evaluation 会话失败：${creation.status()}`);
      sid = (await creation.json()).id;
      report.session_id = sid;
      const panel = page.getByRole("region", {
        name: "新习题引导讲解",
        exact: true,
      });
      await panel.waitFor();
      async function idle() {
        // 使用现有全局忙碌状态，避免把输入框清空或文字变化误当作后端完成。
        await page.waitForFunction(() => {
          const button = document.querySelector("button.new-session");
          return Boolean(button && !button.disabled);
        });
      }
      await idle();
      const before = await api(`/sessions/${sid}`);
      assert(before.guidance, "教材 /practice 没有进入通用 guidance 工作区");
      assert.equal(before.guidance.problem.stem, target.pack.problem.stem);
      assert(!Object.hasOwn(before, "tutoring"), "服务仍返回退役方程工作区");
      assert(!Object.hasOwn(before.guidance, "reference_solution"));
      assert(!Object.hasOwn(before.guidance, "analysis"));
      assert.equal(await page.locator("#equation-step").count(), 0);
      report.checks.push(
        "教材题通过原练习入口启动通用 GuidancePanel，没有专用方程输入或私有参考解",
      );
      const followup =
        "我还不理解第一步为什么成立，请结合题目条件解释，并先让我尝试一步。";
      report.simulated_student_followup = followup;
      const composer = page.getByLabel("向学习助手提问", { exact: true });
      await composer.fill(followup);
      await page.getByRole("button", { name: "发送消息", exact: true }).click();
      await page.waitForFunction(
        (text) =>
          [...document.querySelectorAll(".conversation .user-message")].some(
            (node) => node.textContent.includes(text),
          ),
        followup,
      );
      await idle();
      const after = await api(`/sessions/${sid}`);
      assert(
        after.guidance.turn_count > before.guidance.turn_count,
        "自由追问未推进通用讲题工作区",
      );
      assert(
        after.messages.some(
          (message) => message.role === "user" && message.content === followup,
        ),
        "学生追问原文未持久化",
      );
      assert.equal(
        after.guidance.id,
        before.guidance.id,
        "自由追问错误地启动了另一个工作区",
      );
      report.checks.push(
        "自然语言解释追问通过原 messages 通道续接，原文和通用工作区状态持久化",
      );
      await page.reload();
      await panel.waitFor();
      await idle();
      const restored = await api(`/sessions/${sid}`);
      assert.equal(restored.guidance.id, after.guidance.id);
      assert.equal(restored.guidance.version, after.guidance.version);
      assert.equal(restored.guidance.problem.stem, target.pack.problem.stem);
      assert.equal(
        await page.getByLabel("向学习助手提问", { exact: true }).count(),
        1,
      );
      const roles = await page
        .locator(".conversation .message")
        .evaluateAll((nodes) =>
          nodes.map((node) =>
            node.classList.contains("user-message") ? "user" : "assistant",
          ),
        );
      assert(roles.includes("user") && roles.includes("assistant"));
      assert(
        await page
          .locator(".conversation .user-message")
          .filter({ hasText: followup })
          .count(),
      );
      report.checks.push(
        "刷新恢复原教材题、同一个引导工作区、自由输入及师生消息",
      );
      const runs = (await api(`/sessions/${sid}/runs`)).items || [];
      report.agent_runs = runs;
      report.metrics = {
        model_runs: runs.length,
        failed_runs: runs.filter((run) => run.status !== "completed").length,
        prompt_tokens: runs.reduce(
          (total, run) => total + (run.prompt_tokens || 0),
          0,
        ),
        completion_tokens: runs.reduce(
          (total, run) => total + (run.completion_tokens || 0),
          0,
        ),
      };
      assert(
        runs.some((run) => run.status === "completed" && run.prompt_tokens > 0),
        "未找到真实模型完成回执",
      );
      assert(
        !report.requests.some((request) => request.path.endsWith("/tutoring")),
        "页面仍调用退役方程接口",
      );
      assert(
        report.requests.some((request) => request.path.endsWith("/practice")),
      );
      assert(
        report.requests.some((request) => request.path.endsWith("/messages")),
      );
      await page.screenshot({
        path: path.join(root, "data/reports/textbook_tutoring_ui.png"),
        fullPage: false,
      });
    }
    assert.equal(report.page_errors.length, 0, report.page_errors.join("\n"));
    report.complete = !catalogOnly;
    report.status = catalogOnly
      ? "catalog_only_passed_guidance_not_run"
      : "passed";
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    report.status = "failed";
    report.error = String(error.stack || error);
    if (page)
      await page
        .screenshot({
          path: path.join(root, "data/reports/textbook_ui_failure.png"),
          fullPage: false,
        })
        .catch(() => {});
    throw error;
  } finally {
    report.finished_at = new Date().toISOString();
    await fs.writeFile(
      reportPath,
      JSON.stringify(report, null, 2) + "\n",
      "utf8",
    );
    await browser.close();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
