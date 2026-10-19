"""Turn a run_turn result into the JSON the workbench renders (pure functions, no I/O).

Totals are not computed here: the page sums them from the plan rows it shows, so a figure shown twice is
always one value. This module only labels, groups and re-checks what the planner already produced.
"""

import contextlib
import io
from typing import Any

from tripmate_planner.spec.compile_dsl import compile_dsl_tagged
from tripmate_planner.spec.tripspec import TripSpec

# Official commonsense functions (chinatravel.symbol_verification.commonsense_constraint), in check order.
COMMONSENSE_GROUPS: list[tuple[str, str]] = [
    ("Is_activity_grounded", "活动类型与字段完整"),
    ("Is_intercity_transport_correct", "城际交通：车次、时刻与票价一致"),
    ("Is_attractions_correct", "景点：开放时间内、票价正确、不重复"),
    ("Is_hotels_correct", "酒店：价格与房型正确，多日行程有住宿"),
    ("Is_restaurants_correct", "餐饮：营业时间与用餐时段，不重复"),
    ("Is_transport_correct", "市内交通：与路线工具的结果一致"),
    ("Is_time_correct", "时间：每项有起止，前后不冲突"),
    ("Is_space_correct", "位置：上一站到下一站有交通衔接"),
]

CONSTRAINT_LABELS = {
    "days": "天数", "people": "人数", "tickets": "门票和车票按人数购买", "cars": "打车车辆数",
    "rooms": "房间数", "room_type": "房型", "budget": "总预算", "meal": "人均餐费上限",
    "intercity": "城际交通方式", "innercity": "市内交通方式", "must_visit": "必去景点",
    "must_eat_at": "指定餐厅", "must_stay_at": "指定酒店", "attraction_types": "景点类型",
    "cuisines": "菜系", "avoid_cuisines": "忌口", "hotel_features": "酒店特色",
}

FIELD_LABELS = [
    ("start_city", "出发城市"), ("target_city", "目的地"), ("days", "天数"), ("people_number", "人数"),
    ("budget_total", "总预算"), ("meal_price_max_per_person", "人均餐费上限"), ("intercity_go", "去程交通"),
    ("intercity_back", "返程交通"), ("innercity_mode", "市内交通"), ("rooms", "房间数"), ("room_type", "房型"),
    ("must_visit", "必去景点"), ("must_eat_at", "指定餐厅"), ("must_stay_at", "指定酒店"),
    ("attraction_types", "景点类型"), ("cuisines", "想吃的菜系"), ("avoid_cuisines", "忌口"), ("foods", "想吃的菜"),
    ("hotel_features", "酒店特色"), ("pace", "节奏"), ("notes", "其他说明"),
]
SOURCE_LABELS = {"rule": "规则解析", "llm": "LLM 理解", "memory": "长期偏好", "default": "默认值"}
VALUE_LABELS = {"train": "火车", "airplane": "飞机", "metro": "地铁", "taxi": "打车", "walk": "步行",
                "relaxed": "轻松", "normal": "适中", "intense": "紧凑"}
PREF_LABELS = {"innercity_mode": "市内交通", "intercity_mode": "城际交通", "cuisines_prefer": "偏好菜系",
               "cuisines_avoid": "忌口", "hotel_features": "酒店特色", "room_type": "房型", "pace": "节奏"}
NODE_LABELS = {"load_memory": "读取记忆", "rewrite": "理解需求", "apply_memory": "合并记忆", "compile": "编译约束",
               "retrieve": "检索候选", "plan": "规划行程", "verify": "规则校验", "repair": "修复",
               "reflect": "反思重解析", "confirm_memory": "偏好确认", "save_memory": "保存记忆", "finalize": "整理结果"}


def value_text(key: str, value: Any) -> str:
    if isinstance(value, list):
        return "、".join(value_text(key, v) for v in value)
    if key in ("budget_total", "meal_price_max_per_person"):
        return f"{float(value):,.2f} 元"
    if key == "days":
        return f"{value} 天"
    if key == "people_number":
        return f"{value} 人"
    if key == "rooms":
        return f"{value} 间"
    if key == "room_type":
        return "一张床" if int(value) == 1 else "两张床"
    return VALUE_LABELS.get(str(value), str(value))


def spec_rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The fields the system understood, in reading order, with where each value came from."""
    sources = spec.get("field_sources") or {}
    rows = []
    for key, label in FIELD_LABELS:
        value = spec.get(key)
        if value in (None, [], ""):
            continue
        if key == "pace" and value == "normal" and sources.get(key, "default") == "default":
            continue
        source = sources.get(key, "default")
        rows.append({"key": key, "label": label, "value": value_text(key, value), "source": source,
                     "source_label": SOURCE_LABELS.get(source, source)})
    return rows


def commonsense_report(plan: dict[str, Any] | None, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the eight official commonsense functions on the final plan and keep their grouping."""
    from chinatravel.symbol_verification import commonsense_constraint as cc

    if not plan:
        return [{"id": fid, "label": label, "passed": None, "failed": []} for fid, label in COMMONSENSE_GROUPS]
    symbolic = {"start_city": spec.get("start_city"), "target_city": spec.get("target_city"),
                "people_number": spec.get("people_number") or 1, "days": spec.get("days")}
    out = []
    for fid, label in COMMONSENSE_GROUPS:
        func = getattr(cc, fid)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                table, _info = func(symbolic, plan, verbose=False)
            failed = [str(c) for c in table.columns if int(table[c].loc[0])]
        except Exception as exc:  # the official function itself failed on this plan
            failed = [f"{type(exc).__name__}"]
        out.append({"id": fid, "label": label, "passed": not failed, "failed": failed})
    return out


def constraint_rows(spec: dict[str, Any], spec_failed: list[str]) -> list[dict[str, Any]]:
    """One row per kind of requirement compiled from the TripSpec, passed unless the last check failed it."""
    try:
        tagged = compile_dsl_tagged(TripSpec.model_validate(spec))
    except Exception:
        return []
    seen: list[str] = []
    for tag, _code in tagged:
        if tag not in seen:
            seen.append(tag)
    failed = set(spec_failed)
    return [{"tag": t, "label": CONSTRAINT_LABELS.get(t, t), "passed": t not in failed} for t in seen]


def repair_rows(trace: dict[str, Any]) -> list[dict[str, Any]]:
    history = trace.get("verify_history") or []
    actions = trace.get("repair_actions") or []
    rows = []
    for i, entry in enumerate(history[:-1]):  # the last entry is the final check, not followed by a repair
        rows.append({"round": entry.get("round", i) + 1, "reflect": bool(entry.get("reflect")),
                     "failures": entry.get("n_failures", 0),
                     "actions": [describe_action(a) for a in (actions[i] if i < len(actions) else [])]})
    return rows


def describe_action(action: str) -> str:
    if action.startswith("blacklist attraction"):
        return "换掉出问题的景点"
    if action.startswith("blacklist restaurant"):
        return "换掉出问题的餐厅"
    if action.startswith("blacklist intercity"):
        return "换一班城际交通或换一家酒店"
    if action.startswith("mode_override="):
        return "市内交通改为" + VALUE_LABELS.get(action.split("=", 1)[1], action.split("=", 1)[1])
    if action.startswith("price_caps="):
        return "压低酒店、餐饮和门票的价格上限"
    if action.startswith("force "):
        return "把要求的" + CONSTRAINT_LABELS.get(action[6:], action[6:]) + "强制排进行程"
    return action


def run_rows(trace: dict[str, Any]) -> dict[str, Any]:
    nodes: dict[str, float] = {}
    for n in trace.get("nodes") or []:
        nodes[n["name"]] = nodes.get(n["name"], 0.0) + float(n["ms"])
    llm = trace.get("llm") or []
    api = [r for r in llm if not r.get("local_cache_hit")]
    return {"total_ms": float(trace.get("total_ms") or 0.0),
            "nodes": [{"name": k, "label": NODE_LABELS.get(k, k), "ms": round(v, 1)}
                      for k, v in sorted(nodes.items(), key=lambda kv: -kv[1]) if v >= 1.0],
            "llm_calls": len(llm), "llm_api_calls": len(api),
            "prompt_tokens": sum(int(r.get("prompt_tokens", 0)) for r in llm),
            "completion_tokens": sum(int(r.get("completion_tokens", 0)) for r in llm)}


def title_of(spec: dict[str, Any]) -> str:
    city, days = spec.get("target_city"), spec.get("days")
    if city and days:
        return f"{city} {days} 日行程"
    return f"{city}行程" if city else "新行程"


def preference_rows(prefs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"key": p["key"], "label": PREF_LABELS.get(p["key"], p["key"]), "value": p["value"],
             "value_text": value_text(p["key"], p["value"]), "evidence": p.get("evidence", ""),
             "valid_from": p.get("valid_from", "")} for p in prefs]


def history_rows(entries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Every version of every preference, newest first: the validity chain the memory keeps instead of overwriting."""
    rows = []
    for key, e in entries:
        status = "已忘记" if e.get("deleted_at") else "已被新值替换" if e.get("superseded_by") else "生效中"
        rows.append({"key": key, "label": PREF_LABELS.get(key, key), "value_text": value_text(key, e["value"]),
                     "valid_from": e["valid_from"], "valid_to": e.get("valid_to") or e.get("deleted_at"), "status": status})
    return sorted(rows, key=lambda r: r["valid_from"], reverse=True)


def build_result(*, plan: dict[str, Any] | None, status: str, spec: dict[str, Any], trace: dict[str, Any],
                 candidates: list[dict[str, Any]]) -> dict[str, Any]:
    history = trace.get("verify_history") or []
    last = history[-1] if history else {}
    checks = commonsense_report(plan, spec)
    constraints = constraint_rows(spec, list(last.get("spec_failed") or []))
    return {"status": status, "title": title_of(spec), "plan": plan or None, "spec": spec_rows(spec),
            "budget": spec.get("budget_total"), "people": spec.get("people_number"),
            "start_city": spec.get("start_city"), "target_city": spec.get("target_city"), "days": spec.get("days"),
            "commonsense": checks, "constraints": constraints, "repairs": repair_rows(trace),
            "reflect_used": bool(trace.get("reflect_used")), "run": run_rows(trace),
            "issues": list(trace.get("spec_issues") or []),
            "candidates": [{**c, "label": PREF_LABELS.get(c["key"], c["key"]), "value_text": value_text(c["key"], c["value"]),
                            "decision": None} for c in candidates]}
