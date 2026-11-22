"""机器报告、逐题网页与来源图片复核包。"""

import csv
import html
import io
import json
import math
import statistics
import base64
import mimetypes
from functools import lru_cache
from pathlib import Path
from .config import HOME, experiments, INTENTS
from .files import read_json, read_rows, write_json, atomic_text, verify_freeze
from .metrics import aggregate, paired_bootstrap
from .ragas_adapter import compare
from .costs import summarize_costs

STYLE = "body{max-width:1250px;margin:auto;padding:24px;font:16px system-ui;color:#20242c}table{border-collapse:collapse;width:100%;margin:16px 0}td,th{padding:8px;border:1px solid #ccd2da;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{max-width:280px;max-height:220px}article,details{border:1px solid #ccd2da;padding:12px;margin:12px 0}.note{background:#fff3d9;padding:12px}"


def page(title, body):
    return f"<!doctype html><html lang='zh'><meta charset='utf-8'><title>{html.escape(title)}</title><style>{STYLE}</style><h1>{html.escape(title)}</h1>{body}</html>"


@lru_cache(maxsize=512)
def image_data(location):
    path = Path(location)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def images(assets):
    return "".join(
        f'<a href="{Path(a["location"]).resolve().as_uri()}"><img loading="lazy" src="{image_data(a["location"])}" alt="公开题图"></a>'
        for a in assets
    )


def review_package(dataset):
    dataset = Path(dataset)
    corpus = {r["id"]: r for r in read_rows(dataset / "corpus.jsonl")}
    queries = read_rows(dataset / "queries.jsonl")
    qrels = read_rows(dataset / "qrels.jsonl")
    links = []
    for q in queries:
        body = "<p class='note'>AI银标，尚无人工复标；来源题族划分也需检查近重复。</p>"
        body += f"<p>{html.escape(q['text'])}</p>" + images(q["assets"])
        rows = [r for r in qrels if r["query_id"] == q["id"]]
        rows.sort(
            key=lambda r: (r["grade"] is not None, -(r["grade"] or 0), r["question_id"])
        )
        for r in rows:
            candidate = corpus[r["question_id"]]
            body += f"<details><summary>{html.escape(r['question_id'])} / 等级 {r['grade']} / 复判 {r.get('second') is not None}</summary><p>{html.escape(candidate['text'])}</p>"
            body += (
                images(candidate["assets"])
                + "<pre>"
                + html.escape(json.dumps(r, ensure_ascii=False, indent=2))
                + "</pre></details>"
            )
        filename = q["id"] + ".html"
        atomic_text(
            dataset / "review" / filename, page(q["id"] + " / " + q["intent"], body)
        )
        links.append(
            f'<li><a href="{filename}">{html.escape(q["id"])}</a> {q["split"]} / {q["input_kind"]} / {q["intent"]}</li>'
        )
    inventory = "<details><summary>候选题库及冻结转录复核</summary>"
    for row in corpus.values():
        inventory += (
            f"<details><summary>{row['id']} / {html.escape(row['transcription_status'])}</summary><pre>{html.escape(row['text'])}</pre>"
            + images(row["assets"])
            + "</details>"
        )
    inventory += "</details>"
    atomic_text(
        dataset / "review/index.html",
        page(
            "数学检索银标复核包",
            "<p class='note'>未标注不等于负例；完整答案和原始解析不进入此页面。</p><ul>"
            + "".join(links)
            + "</ul>"
            + inventory,
        ),
    )
    return {
        "queries": len(queries),
        "labeled_pairs": len(qrels),
        "path": str(dataset / "review/index.html"),
    }


def report(run_id, *, ragas_python=None, use_ragas=True):
    directory = HOME / "runs" / run_id
    config = read_json(directory / "run.json")
    if not config:
        raise ValueError("运行记录不存在")
    dataset = Path(config["dataset"])
    frozen = verify_freeze(dataset)
    if frozen["dataset_hash"] != config["dataset_hash"]:
        raise ValueError("运行与数据集版本不一致")
    queries = read_rows(dataset / "queries.jsonl")
    corpus = {r["id"]: r for r in read_rows(dataset / "corpus.jsonl")}
    qrels = read_rows(dataset / "qrels.jsonl")
    records = [
        read_json(p)
        for e in experiments()
        for p in sorted((directory / e.id).glob("*.json"))
    ]
    completed = [r for r in records if r["status"] == "completed"]
    expected = len(queries) * len(experiments())
    result = {
        "run_id": run_id,
        "label_kind": "silver_not_human_reviewed",
        "status": "complete" if len(completed) == expected else "partial",
        "expected": expected,
        "completed": len(completed),
        "failed": len(records) - len(completed),
        "missing": expected - len(records),
        "dataset_hash": frozen["dataset_hash"],
        "recall_notice": "同构和同方法为Pooled Recall；未穷尽全部相关题；均衡抽样不代表线上流量",
        "limitations": [
            "AI转录和银标共享模型可能产生相关偏差",
            "银标依据冻结图示转录和隔离解析，原始像素留在复核包；漏读图示可能影响标注",
            "语义近重复题族需人工复核",
            "E4包含编码器互补效应，不能把全部提升归因于像素",
            "固定每通道50候选，E4总候选预算可能更大",
        ],
        "by_split": {},
        "by_intent": {},
        "by_input": {},
        "by_module": {},
        "retrieved_image_fraction": {},
        "confidence_intervals": {},
        "timing": {},
    }
    for split in ("development", "test"):
        subset = [r for r in completed if r["split"] == split]
        result["by_split"][split] = {
            e.id: aggregate([r["metrics"] for r in subset if r["experiment"] == e.id])
            for e in experiments()
        }
    test = [r for r in completed if r["split"] == "test"]
    for field, target, values in (
        ("intent", "by_intent", INTENTS),
        ("input_kind", "by_input", ("text", "image")),
        ("module", "by_module", sorted({q["module"] for q in queries})),
    ):
        result[target] = {
            value: {
                e.id: aggregate(
                    [
                        r["metrics"]
                        for r in test
                        if r[field] == value and r["experiment"] == e.id
                    ]
                )
                for e in experiments()
            }
            for value in values
        }
    # 候选题模态单独拆分召回分母，不能拿查询是否带图代替候选题模态。
    result["candidate_modality_recall"] = {}
    for image in (False, True):
        by_exp = {}
        for e in experiments():
            values = []
            for r in test:
                if r["experiment"] != e.id:
                    continue
                refs = {
                    j["question_id"]
                    for j in qrels
                    if j["query_id"] == r["query_id"]
                    and j["grade"] is not None
                    and j["grade"] >= 2
                    and corpus[j["question_id"]]["has_image"] == image
                }
                if refs:
                    values.append(
                        len(refs & {h["id"] for h in r["result"]["rankings"][:10]})
                        / len(refs)
                    )
            by_exp[e.id] = {
                "queries_with_reference": len(values),
                "pooled_recall_at_10": statistics.mean(values) if values else None,
            }
        result["candidate_modality_recall"]["image" if image else "text"] = by_exp
    if result["status"] == "complete":
        for a, b in (("E3", "E1"), ("E3", "E2"), ("E4", "E3")):
            result["confidence_intervals"][a + "-" + b] = {
                metric: paired_bootstrap(test, a, b, metric)
                for metric in ("recall_at_10", "ndcg_at_10", "precision_at_10")
            }
    observations = [
        o
        for path in (directory / "timing").glob("*.json")
        for o in read_json(path).get("observations", [])
    ]
    for e in experiments():
        times = sorted(o["ms"] for o in observations if o["experiment"] == e.id)
        result["timing"][e.id] = {
            "samples": len(times),
            "p50_ms": statistics.median(times) if times else None,
            "p95_ms": (
                times[min(len(times) - 1, math.ceil(len(times) * 0.95) - 1)]
                if times
                else None
            ),
            "scope": "一次预热后3轮，预计算向量，不包含转录或在线编码耗时",
        }
    result["ragas"] = (
        compare(completed, qrels, directory, ragas_python)
        if use_ragas and completed
        else {"status": "not_run"}
    )
    result["cost"] = summarize_costs(dataset)
    result["annotation_summary"] = {
        "pairs": len(qrels),
        "positive_pairs": sum(
            r["grade"] is not None and r["grade"] >= 2 for r in qrels
        ),
        "second_judgments": sum(bool(r.get("second")) for r in qrels),
        "adjudications": sum(bool(r.get("adjudication")) for r in qrels),
        "hard_negative_pairs": sum(bool(r.get("near_miss")) for r in qrels),
        "human_reviewed_pairs": sum(bool(r.get("human_reviewed")) for r in qrels),
    }
    if not result["annotation_summary"]["hard_negative_pairs"]:
        result["limitations"].append(
            "模型未标出任何near_miss，困难负例占比为0且无诊断区分力；不能据此证明没有困难误召回"
        )
    result["preparation_audit"] = {
        "source_questions": len(corpus),
        "final_query_families": len({q["source_group"] for q in queries}),
        "replaced_families": len(
            read_json(dataset / "review/replacement_history.json", [])
        ),
        "rule": "冻结前仅依据全候选域无正例证据替换整族，未按检索指标选题；旧证据保留review/replaced",
    }
    selection = read_json(dataset / "subset_selection.json")
    if selection:
        result["preparation_audit"]["subset_selection"] = selection
        result["preparation_audit"]["rule"] = selection["selection_basis"]
        result["limitations"].extend(
            [
                "24条查询按正例可用性筛选，属于条件化流程验证集；不能代表所有查询的表现",
                selection["limitation"],
                "测试集只有12条查询、6个来源题族，bootstrap区间仅作描述，不作稳定优劣结论",
            ]
        )
    result["candidate_resources"] = {}
    for e in experiments():
        selected = [r for r in completed if r["experiment"] == e.id]
        counts = [r["result"]["candidate_count"] for r in selected]
        failures = sum(
            r["experiment"] == e.id and r["status"] != "completed" for r in records
        )
        result["candidate_resources"][e.id] = {
            "completed_queries": len(selected),
            "failed_queries": failures,
            "missing_queries": len(queries) - len(selected) - failures,
            "union_mean": statistics.mean(counts) if counts else None,
            "union_min": min(counts) if counts else None,
            "union_max": max(counts) if counts else None,
            "per_channel_mean": {
                name: (
                    statistics.mean(
                        [len(r["result"]["channel_rankings"][name]) for r in selected]
                    )
                    if selected
                    else None
                )
                for name in e.channels
            },
            "es_requests_per_single_chunk_query": len(e.channels),
        }
    by_query = {(r["experiment"], r["query_id"]): r for r in test}
    differences = []
    for q in queries:
        a, b = by_query.get(("E4", q["id"])), by_query.get(("E3", q["id"]))
        if a and b:
            differences.append(
                {
                    "query_id": q["id"],
                    "intent": q["intent"],
                    "input_kind": q["input_kind"],
                    "ndcg_delta": a["metrics"]["ndcg_at_10"]
                    - b["metrics"]["ndcg_at_10"],
                    "recall_delta": a["metrics"]["recall_at_10"]
                    - b["metrics"]["recall_at_10"],
                }
            )
    result["gain_examples"] = sorted(
        [d for d in differences if d["ndcg_delta"] > 0], key=lambda d: -d["ndcg_delta"]
    )[:10]
    result["regression_examples"] = sorted(
        [d for d in differences if d["ndcg_delta"] < 0], key=lambda d: d["ndcg_delta"]
    )[:10]
    result["review"] = review_package(dataset)
    prefix = HOME / "reports" / run_id
    write_json(prefix.with_suffix(".json"), result)
    columns = [
        "experiment",
        "query_id",
        "split",
        "intent",
        "input_kind",
        "source_group",
    ] + sorted({k for r in completed for k in r["metrics"]})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in completed:
        writer.writerow({**{k: row[k] for k in columns if k in row}, **row["metrics"]})
    atomic_text(prefix.with_suffix(".csv"), stream.getvalue())
    body = (
        "<p class='note'>银标诊断基准；不代表全题库真实召回率或学习收益。状态："
        + result["status"]
        + "</p>"
    )
    if selection:
        body += "<p class='note'>100道候选题、24条查询；开发/测试各12条。按正例可用性筛选，测试仅6个题族，且父版本参与过标注诊断。结果仅用于小规模流程验证。</p>"
    metric_columns = [
        ("recall_at_5", "Recall/Pooled@5"),
        ("recall_at_10", "Recall/Pooled@10"),
        ("recall_at_20", "Recall/Pooled@20"),
        ("ndcg_at_10", "nDCG@10"),
        ("precision_at_5", "Precision@5"),
        ("precision_at_10", "Precision@10"),
    ]
    for split, title in (("test", "测试集主表"), ("development", "开发集（单独展示）")):
        body += (
            "<h2>"
            + title
            + "</h2><p>查询宏平均；同构、同方法的召回率相对于已标注候选池。</p>"
        )
        body += (
            "<table><tr><th>实验</th><th>查询数</th>"
            + "".join("<th>" + name + "</th>" for _, name in metric_columns)
            + "</tr>"
        )
        for e in experiments():
            values = result["by_split"][split][e.id]
            body += f"<tr><td>{e.id}</td><td>{values['queries']}</td>"
            body += (
                "".join(
                    "<td>"
                    + (
                        f"{values[key]:.4f}"
                        if values.get(key) is not None
                        else "未完成"
                    )
                    + "</td>"
                    for key, _ in metric_columns
                )
                + "</tr>"
            )
        body += "</table>"
    body += (
        "<h2>标注与RAGAS核验</h2><p>"
        + html.escape(
            f"查询—题目对 {len(qrels)}；正例 {result['annotation_summary']['positive_pairs']}；复判 {result['annotation_summary']['second_judgments']}；分歧裁决 {result['annotation_summary']['adjudications']}。RAGAS状态 {result['ragas']['status']}，核对 {result['ragas'].get('comparisons', 0)} 组。"
        )
        + "</p><ul>"
        + "".join("<li>" + html.escape(v) + "</li>" for v in result["limitations"])
        + "</ul>"
    )
    body += "<h2>配对差值与95%区间</h2><p>测试集按来源题族重采样2,000次；小样本区间仅作描述。</p><table><tr><th>比较</th><th>指标</th><th>差值</th><th>95%区间</th></tr>"
    for pair, values in result["confidence_intervals"].items():
        for metric, value in values.items():
            if value.get("available"):
                body += f"<tr><td>{pair}</td><td>{metric}</td><td>{value['difference']:.4f}</td><td>[{value['ci95'][0]:.4f}, {value['ci95'][1]:.4f}]</td></tr>"
    body += "</table><h2>检索阶段耗时与资源</h2><p>相同预计算查询向量、预热一次、轮换实验顺序重复三轮。耗时不含转录、编码和标注。</p><table><tr><th>实验</th><th>计时次数</th><th>P50毫秒</th><th>P95毫秒</th><th>候选并集均值</th><th>成功/失败/缺失</th></tr>"
    for e in experiments():
        timing, resource = result["timing"][e.id], result["candidate_resources"][e.id]
        fmt = lambda v: f"{v:.3f}" if v is not None else "未完成"
        body += f"<tr><td>{e.id}</td><td>{timing['samples']}</td><td>{fmt(timing['p50_ms'])}</td><td>{fmt(timing['p95_ms'])}</td><td>{fmt(resource['union_mean'])}</td><td>{resource['completed_queries']}/{resource['failed_queries']}/{resource['missing_queries']}</td></tr>"
    body += (
        "</table><h2>模型用量</h2><p>"
        + html.escape(result["cost"]["inherited_notice"])
        + "</p><p>"
        + html.escape(result["cost"]["notice"])
        + "</p><table><tr><th>阶段</th><th>记录HTTP尝试</th><th>Tokens</th><th>缓存命中</th><th>失败操作</th><th>用量缺失</th></tr>"
    )
    for name, value in result["cost"]["groups"].items():
        body += (
            "<tr><td>"
            + html.escape(name)
            + "</td>"
            + "".join(
                f"<td>{value[k]}</td>"
                for k in (
                    "http_attempts_recorded",
                    "total_tokens",
                    "cache_hits",
                    "failed_operations",
                    "usage_missing_operations",
                )
            )
            + "</tr>"
        )
    body += "</table><h2>E4相对E3的案例</h2><p>按测试查询nDCG差值列出增益和退化，打开链接核对原图与银标依据。</p><ul>"
    for kind in ("gain_examples", "regression_examples"):
        for value in result[kind]:
            link = (
                (dataset / "review" / (value["query_id"] + ".html")).resolve().as_uri()
            )
            body += f'<li><a href="{link}">{value["query_id"]}</a> / {value["intent"]} / ΔnDCG={value["ndcg_delta"]:.4f} / ΔRecall@10={value["recall_delta"]:.4f}</li>'
    body += "</ul>"
    body += (
        "<details><summary>分层指标、置信区间、增退案例、计时与成本完整记录</summary><pre>"
        + html.escape(json.dumps(result, ensure_ascii=False, indent=2))
        + "</pre></details>"
    )
    for q in queries:
        body += (
            f"<details><summary>{html.escape(q['id']+' / '+q['intent'])}</summary><p>{html.escape(q['text'])}</p>"
            + images(q["assets"])
        )
        body += f'<a href="{(dataset/"review"/(q["id"]+".html")).resolve().as_uri()}">查看盲标证据与复核</a><table><tr><th>实验</th><th>前20排名</th><th>指标</th></tr>'
        for row in completed:
            if row["query_id"] == q["id"]:
                body += f"<tr><td>{row['experiment']}</td><td>{html.escape(', '.join(r['id'] for r in row['result']['rankings'][:20]))}</td><td><pre>{html.escape(json.dumps(row['metrics'],ensure_ascii=False))}</pre></td></tr>"
        body += "</table></details>"
    atomic_text(prefix.with_suffix(".html"), page("四组习题检索对照", body))
    return result
