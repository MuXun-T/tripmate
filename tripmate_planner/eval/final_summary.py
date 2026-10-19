"""T5.4: build reports/final/summary.md from the one-shot test runs (and dev runs for the ablation reference).

Every number in the summary is read from runs/<run_id>/metrics.json (or retrieval/extraction metrics) and the
cost ledger; nothing is typed in by hand except the two paper reference values, which are marked as such.
"""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from tripmate_planner.eval.report import table as dev_table
from tripmate_planner.eval.stats import bootstrap_ci, mcnemar_exact
from tripmate_planner.paths import COST_LEDGER, REPORTS, RUNS

PAPER = "arXiv 2412.13682v4，human-154；模型与本项目不同，只作参考，不做显著性检验"
PAPER_REACT_FPR, PAPER_NESY_FPR, PAPER_REACT_INPUT_K = 2.6, 37.0, 1058


def _load(run_id: str, name: str = "metrics.json") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RUNS / run_id / name).read_text(encoding="utf-8"))
    return data


def _ledger() -> list[dict[str, Any]]:
    return [json.loads(line) for line in COST_LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]


def _main_table(rows: list[tuple[str, dict[str, Any]]], ours: dict[str, Any]) -> str:
    out = ["| 方法 | run_id | n | DR | EPR-micro | EPR-macro | LPR-micro | LPR-macro | C-LPR | FPR [95% CI] | 本系统 vs 该方法 b01/b10, p |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, m in rows:
        o, ci = m["official"], m["ci95_pct"]["FPR"]
        cmp = "-"
        if m is not ours:
            common = [u for u in ours["per_uid"] if u in m["per_uid"]]
            b01, b10, p = mcnemar_exact([m["per_uid"][u]["final"] for u in common], [ours["per_uid"][u]["final"] for u in common])
            cmp = f"{b01}/{b10}, p={p:.3g}"
        out.append(f"| {label} | {m['run_id']} | {m['n']} | {o['DR']:.2f} | {o['EPR_micro']:.2f} | {o['EPR_macro']:.2f} | "
                   f"{o['LPR_micro']:.2f} | {o['LPR_macro']:.2f} | {o['C_LPR']:.2f} | {o['FPR']:.2f} [{ci[1]:.2f}, {ci[2]:.2f}] | {cmp} |")
    out.append(f"| 论文 ReAct（one-shot，DeepSeek） | - | 154 | - | - | - | - | - | - | {PAPER_REACT_FPR:.1f} | 参考值 |")
    out.append(f"| 论文 NeSy Planning（DeepSeek-V3） | - | 154 | - | - | - | - | - | - | {PAPER_NESY_FPR:.1f} | 参考值 |")
    return "\n".join(out)


def _cost_rows(ledger: list[dict[str, Any]], runs: list[tuple[str, str, int]]) -> str:
    out = ["| 方法 | run_id | 实际 API 调用/条 | 输入 token/条 | 其中 DeepSeek 缓存命中 | 输出 token/条 | 费用/条（标准价，元） | 费用/条（高峰价，元） | 本次合计（高峰价，元） |",
           "|---|---|---|---|---|---|---|---|---|"]
    for label, rid, n in runs:
        rows = [r for r in ledger if r["run_id"] == rid and not r["local_cache_hit"]]
        if not rows:
            out.append(f"| {label} | {rid} | 0 | 0 | 0 | 0 | 0 | 0 | 0 |")
            continue
        prompt, hit = sum(r["prompt_tokens"] for r in rows), sum(r["hit_tokens"] for r in rows)
        comp = sum(r["completion_tokens"] for r in rows)
        std, peak = sum(r["cost_std"] for r in rows), sum(r["cost_peak"] for r in rows)
        out.append(f"| {label} | {rid} | {len(rows) / n:.2f} | {prompt / n:,.0f} | {hit / max(prompt, 1):.1%} | {comp / n:,.0f} | "
                   f"{std / n:.4f} | {peak / n:.4f} | {peak:.3f} |")
    return "\n".join(out)


def _fail_table(named: list[tuple[str, dict[str, Any]]]) -> str:
    keys: list[str] = []
    for _, m in named:
        keys += [k for k in m["commonsense_fail_counts"] if k not in keys]
    out = ["| 常识规则（失败条数） | " + " | ".join(label for label, _ in named) + " |", "|---|" + "---|" * len(named)]
    for k in sorted(keys, key=lambda k: -max(m["commonsense_fail_counts"].get(k, 0) for _, m in named)):
        out.append(f"| {k} | " + " | ".join(str(m["commonsense_fail_counts"].get(k, 0)) for _, m in named) + " |")
    return "\n".join(out)


def _retrieval_table(r: dict[str, Any]) -> str:
    out = ["| 配置 | 类型子查询 n | nDCG@10 [95% CI] | Success@10 | P@10 | 定位子查询 n | Success@1 | MRR@10 |",
           "|---|---|---|---|---|---|---|---|"]
    for name, m in r["pure"].items():
        t, p = m["type"], m["position"]
        out.append(f"| {name} | {t['n']} | {t['ndcg@10']['mean']:.3f} [{t['ndcg@10']['ci95'][0]:.3f}, {t['ndcg@10']['ci95'][1]:.3f}] | "
                   f"{t['success@10']['mean']:.3f} | {t['p@10']['mean']:.3f} | {p['n']} | {p['success@1']['mean']:.3f} | {p['mrr@10']['mean']:.3f} |")
    out += ["", "| 过滤方式 | 子查询数 | 不足率 | 返回结果违规率 | 成功率 |", "|---|---|---|---|---|"]
    label = {"pre": "召回前过滤（本系统）", "post": "召回后过滤", "none": "不过滤"}
    for k, v in r["filter"].items():
        out.append(f"| {label.get(k, k)} | {v['subqueries']} | {v['shortfall_rate']:.2%} | {v['violation_rate']:.2%} | {v['success@pool']:.2%} |")
    return "\n".join(out)


def _failure_layers(m: dict[str, Any]) -> str:
    per = m["per_uid"]
    n = len(per)
    schema_fail = sum(not v["schema"] for v in per.values())
    comm_fail = sum(v["schema"] and not v["commonsense"] for v in per.values())
    logic_only = sum(v["schema"] and v["commonsense"] and not v["logic"] for v in per.values())
    return "\n".join([
        "| 失败层（互斥，按先后判定） | 条数 | 占比 |", "|---|---|---|",
        f"| 无计划或不符合 schema | {schema_fail} | {schema_fail / n:.1%} |",
        f"| 常识规则失败 | {comm_fail} | {comm_fail / n:.1%} |",
        f"| 常识通过、逻辑约束失败 | {logic_only} | {logic_only / n:.1%} |",
        f"| 全部通过（FPR） | {sum(v['final'] for v in per.values())} | {m['official']['FPR'] / 100:.1%} |",
    ])


def _field_table(m: dict[str, Any]) -> str:
    out = ["| TripSpec 字段（对 oracle 标签） | P | R | F1 | tp / fp / fn |", "|---|---|---|---|---|"]
    for k, v in sorted(m["extraction"]["per_field"].items(), key=lambda kv: kv[1]["f1"]):
        out.append(f"| {k} | {v['precision']:.3f} | {v['recall']:.3f} | {v['f1']:.3f} | "
                   f"{v.get('tp', 0)} / {v.get('fp', 0)} / {v.get('fn', 0)} |")
    return "\n".join(out)


def _rulenesy_note(run_id: str) -> str:
    """RuleNeSy on test was stopped early (see progress.md); describe what the partial log shows."""
    log = RUNS / "final_logs" / "rulenesy.log"
    done = [line for line in log.read_text(encoding="utf-8").splitlines() if re.match(r"^\[\d+/\d+\] ", line)] \
        if log.exists() else []
    if not done:
        return "- 官方 RuleNeSy：未在 test 上运行。"
    secs = [float(m.group(1)) for line in done if (m := re.search(r" ([\d.]+)s$", line))]
    succ = sum("succ=True" in line for line in done)
    return (f"- 官方 RuleNeSy（无 LLM）：在 test 上运行了前 {len(done)} 条后终止，未列入主表。这 {len(done)} 条中搜索成功 {succ} 条，"
            f"每条耗时 {min(secs):.0f}–{max(secs):.0f} s（官方搜索时限 300 s），按此估算全量需要十几个小时；"
            f"它不读自然语言、只用结构化字段，作为参照的价值有限。未写锁文件（run_id {run_id}）。")


def _replay_section() -> str:
    chk = json.loads((REPORTS / "final" / "replay_check.json").read_text(encoding="utf-8"))
    out = [f"命令：`python -m tripmate_planner.eval.replay --tag <后缀>`；回放包 `{chk['pack']}`，每次回放只加载对应运行自己记录的响应。",
           "", "| 原始运行 | 回放运行 | 官方指标一致 | 逐条通过情况一致 | 计划文件不同的条数 | 回放 FPR | 回放 API 费用 |",
           "|---|---|---|---|---|---|---|"]
    notes = []
    for r in chk["runs"]:
        out.append(f"| {r['original']} | {r['replay']} | {'是' if r['official_equal'] else '否'} | "
                   f"{'是' if r['per_uid_equal'] else '否'} | {len(r['plans_differing'])} | {r['official']['FPR']:.2f} | {r['api_cost_peak_replay']} |")
        for f in r["plans_differing"]:
            uid = f.removesuffix(".json")
            t0 = json.loads((RUNS / r["original"] / "traces" / f).read_text(encoding="utf-8"))
            t1 = json.loads((RUNS / r["replay"] / "traces" / f).read_text(encoding="utf-8"))
            hits = all(x["local_cache_hit"] for x in t1.get("llm", []))
            same_llm = [x["purpose"] for x in t0.get("llm", [])] == [x["purpose"] for x in t1.get("llm", [])]
            notes.append(f"- {r['original']} / {uid}：原始 final_status={t0.get('final_status')}，回放 {t1.get('final_status')}；"
                         f"修复 {t0.get('repair_rounds')} 轮、反思 {t0.get('reflect_used')}；回放中 LLM 调用全部命中回放缓存={hits}，"
                         f"调用序列与原始相同={same_llm}。差异来自规划器的墙钟时间预算（time_budget_s="
                         f"{json.loads((RUNS / r['original'] / 'config.json').read_text(encoding='utf-8'))['planner']['time_budget_s']}）："
                         "这条已用完全部修复轮次的难例中，搜索在时间预算内到达的候选随机器负载变化。冻结后不再修改规划器，"
                         "报告中的数字均取原始运行。")
    return "\n".join(out + [""] + notes) if notes else "\n".join(out)


def build(args: argparse.Namespace) -> str:
    ours, react, mt = _load(args.ours), _load(args.react0), _load(args.mt)
    rule = _load(args.rulenesy) if (RUNS / args.rulenesy / "metrics.json").exists() else None
    retr = _load(args.retrieval, "retrieval_metrics.json")
    ledger = _ledger()
    freeze = json.loads((REPORTS / "final" / "FREEZE.json").read_text(encoding="utf-8"))
    per_scope: Counter[str] = Counter()
    for r in ledger:
        per_scope[r["scope"]] += r["cost_peak"]
    total_peak, total_std = sum(per_scope.values()), sum(r["cost_std"] for r in ledger)
    named = [("本系统（full）", ours), ("官方 ReAct0 复现（deepseek-flash，zero-shot）", react)]
    if rule is not None:
        named.append(("官方 RuleNeSy（无 LLM）", rule))
    react_plans = react.get("plans_saved", react["n"])
    mt_uids = list(mt["per_uid"])
    single = [ours["per_uid"][u]["final"] for u in mt_uids]
    multi = [mt["per_uid"][u]["final"] for u in mt_uids]
    sb01, sb10, sp = mcnemar_exact(single, multi)
    s_ci = [round(100 * x, 2) for x in bootstrap_ci(single)]
    lat = ours["latency_ms"]
    lines = [
        "# 最终结果（ChinaTravel human-154 test，只运行一次）",
        "",
        f"- 冻结配置：config_hash `{freeze['config_hash']}`（git `{freeze['git_head_at_freeze'][:7]}`，见 reports/final/FREEZE.json）；test 锁文件见 reports/final/TEST_LOCK_*.json。",
        "- 模型：deepseek-flash，thinking 关闭（`extra_body={\"thinking\":{\"type\":\"disabled\"}}`），temperature 0；本系统与 ReAct0 复现使用同一模型。",
        "- 指标由官方评测函数计算（与官方 CLI 一致，见 reports/p0_metric_consistency.md）；95% CI 为 bootstrap 1,000 次（seed 20261018）；McNemar 为按 uid 配对的双侧精确检验，b01 = 该方法失败且本系统通过的条数，b10 = 该方法通过且本系统失败的条数。",
        "",
        "## 1. 主表（单轮）",
        "",
        _main_table(named, ours),
        "",
        f"- 论文参考值来源：{PAPER}。",
        f"- ReAct0 复现：保存计划 {react_plans}/{react['n']} 条，其中报错 {react.get('plans_with_error', 0)} 条；未保存的条目按空计划计分。",
    ]
    if rule is None:
        lines.append(_rulenesy_note(args.rulenesy))
    if rule is not None:
        lines.append("- RuleNeSy 不调用 LLM：它不读自然语言，直接使用数据集里的结构化字段（出发地、目的地、天数、人数），"
                     "并且在不使用 oracle 翻译时没有逻辑约束，只按常识规则规划。它和本系统的输入信息不同，只作参照。")
    lines += [
        "",
        "## 2. 成本与 token（每条查询平均值）",
        "",
        _cost_rows(ledger, [("本系统（full，单轮）", args.ours, ours["n"]), ("本系统（多轮）", args.mt, mt["n"]),
                            ("官方 ReAct0 复现", args.react0, react["n"])]),
        "",
        f"- 论文中 ReAct（one-shot）每条查询约 {PAPER_REACT_INPUT_K:,}K 输入 token（参考值，{PAPER}）。",
        "- 只统计真正发往 API 的调用（本地缓存命中不计）；“DeepSeek 缓存命中”是 API 端上下文缓存命中的输入 token 占比。",
        "- 调用数和 token 以费用账本（runs/cost_ledger.jsonl）为准，其中包括返回未通过 schema 校验、最终退回规则解析的调用；"
        "trace 不记录这部分，所以 runs/<run_id>/metrics.json 里的每条调用数略低。",
        f"- 项目全部实验累计 API 费用：高峰价核算 {total_peak:.3f} 元，标准价 {total_std:.3f} 元；按 scope（高峰价）："
        + "，".join(f"{k} {v:.3f}" for k, v in sorted(per_scope.items())) + "。",
        "",
        "## 3. 多轮（MT-test，154 段对话，由 human-154 派生）",
        "",
        "| 配置 | run_id | n | DR | EPR-macro | LPR-macro | FPR [95% CI] | 约束保持率 | 含偏好会话的对话 |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| 本系统（多轮） | {mt['run_id']} | {mt['n']} | {mt['official']['DR']:.2f} | {mt['official']['EPR_macro']:.2f} | "
        f"{mt['official']['LPR_macro']:.2f} | {mt['official']['FPR']:.2f} [{mt['ci95_pct']['FPR'][1]:.2f}, {mt['ci95_pct']['FPR'][2]:.2f}] | "
        f"{mt.get('constraint_retention', '-')} | {mt.get('with_preference', '-')} |",
        f"| 同一批 uid 的单轮结果 | {ours['run_id']} | {len(mt_uids)} | - | - | - | {s_ci[0]:.2f} [{s_ci[1]:.2f}, {s_ci[2]:.2f}] | - | - |",
        "",
        f"- 多轮相对单轮：b01/b10 = {sb01}/{sb10}（b01 = 单轮失败而多轮通过，b10 = 单轮通过而多轮失败），p={sp:.3g}。",
        "- 关闭记忆（M1）、关闭长期偏好（M2）的消融只在 dev 上做（reports/p4_mt_dev.md）；test 中含偏好会话的对话很少，跨会话偏好在 test 上基本无法检验。",
        "",
        "## 4. 检索（POI-Ret test，由 oracle 派生）",
        "",
        _retrieval_table(retr),
        "",
        "- 重排（bge-reranker-base）在 dev 上提升了 nDCG@10，但 CPU 上的检索延迟超出 T2.6 的门槛，冻结配置中关闭（reports/p2_retrieval_dev.md）；这里的 hybrid+rerank 行只作组件评测。",
        "",
        "## 5. 常识失败分布（单轮 test）",
        "",
        _fail_table(named),
        "",
        "## 6. 本系统运行概况（单轮 test）",
        "",
        f"- final_status：{ours['final_status']}；修复轮数均值 {ours['repair_rounds_mean']}；反思触发率 {ours['reflect_rate']}。",
        f"- 约束抽取（对 oracle 标签）：micro F1 {ours['extraction']['micro']['f1']:.4f}，精确匹配率 {ours['extraction']['exact_match_rate']:.4f}。",
        f"- DSL 校验最终仍失败的约束类型：{ours['final_spec_fail_counts'] or '无'}。",
        "",
        _failure_layers(ours),
        "",
        _field_table(ours),
        "",
        f"- 端到端延迟 P50 {lat['p50'] / 1000:.1f} s / P95 {lat['p95'] / 1000:.1f} s（4 线程并发；本机有其他常驻负载，绝对值仅供参考，不作为对外数字）。",
        "",
        "## 7. 复现（回放包，不调用 API）",
        "",
        _replay_section(),
        "",
        "## 8. dev 消融（引用，未在 test 上运行）",
        "",
        "详见 reports/p3_dev.md 与 reports/p4_mt_dev.md。主表复制如下（由同一脚本从 dev 运行结果生成）：",
        "",
        dev_table(args.dev_runs, args.dev_runs[0], labels=dict(zip(args.dev_runs, args.dev_labels, strict=True))),
        "",
        "LLM 直接写计划（A9，dev_mini 60 条配对；LLM 只决定活动、顺序和时间，交通与费用由程序补全，不做校验和修复）：",
        "",
        dev_table(["p3-final-full-devmini", "p3-A9_llm_direct-devmini"], "p3-final-full-devmini",
                  labels={"p3-final-full-devmini": "full", "p3-A9_llm_direct-devmini": "A9 LLM 直接写计划"}),
        "",
        "多轮记忆消融（MT dev，60 段对话）：",
        "",
        dev_table(["p4-mt-full-dev", "p4-mt-M1-dev", "p4-mt-M2-dev"], "p4-mt-full-dev",
                  labels={"p4-mt-full-dev": "full（多轮）", "p4-mt-M1-dev": "M1 关闭记忆（只给最后一轮）",
                          "p4-mt-M2-dev": "M2 关闭长期偏好"}),
        "",
        "A6（打开重排）未运行：重排在冻结配置中关闭，按计划只在启用时才做 A6。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ours", default="p5-full-test")
    parser.add_argument("--mt", default="p5-mt-full-test")
    parser.add_argument("--react0", default="p5-react0-test")
    parser.add_argument("--rulenesy", default="p5-rulenesy-test")
    parser.add_argument("--retrieval", default="p5-retrieval-test")
    parser.add_argument("--dev-runs", nargs="+", default=[
        "p3-final-full-dev", "p3-A1_no_repair-dev", "p3-A2_no_spec_verify-dev", "p3-A3_rule_rewrite-dev",
        "p3-A4_bm25-dev", "p3-A5_dense-dev", "p3-A7_postfilter-dev", "p3-A8_no_feature_score-dev"])
    parser.add_argument("--dev-labels", nargs="+", default=[
        "full", "A1 去掉修复环", "A2 去掉 DSL 硬约束校验", "A3 纯规则改写", "A4 只用 BM25", "A5 只用稠密",
        "A7 召回后过滤", "A8 去掉特征打分"])
    parser.add_argument("--out", default=str(REPORTS / "final" / "summary.md"))
    args = parser.parse_args()
    text = build(args)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
