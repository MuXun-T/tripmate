"""T4.4: derive multi-turn dialogues from single-turn queries (nature_language only; no oracle)."""

import argparse
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tripmate_planner.config import load_config
from tripmate_planner.data.splits import load_queries, uid_list
from tripmate_planner.llm.client import build_client
from tripmate_planner.llm.errors import LLMOutputError, LLMRequestError
from tripmate_planner.paths import CONFIGS, DATA

PROMPT = """你要把一条完整的旅行需求改写成多轮对话，用于测试对话记忆。只输出 json。
要求：
1. 不增加、不删除、不改变任何需求；每个需求恰好出现一次。
2. turns 共 2–3 轮。第 1 轮必须包含出发地、目的地、天数、人数（原文有的才写）。其余需求分散到后续轮次。
3. 至少 1 轮使用指代（如“那边”“刚说的那个地方”“人数还是之前说的”），指代对象必须在之前的轮次出现过；把使用指代的轮次序号（从 1 开始）写入 reference_turns。
4. 如果原文中有以下类型的需求之一：市内交通方式、城际交通方式、菜系偏好或忌口、酒店特色、房型，选择其中恰好 1 个，改写成“长期习惯”的一句话放入 preference_session（例如“我平时出门都只坐地铁。”），并且不在 turns 中出现；moved_constraint 写这个需求的原文短语。没有这类需求时 preference_session 为空列表，moved_constraint 为 null。
5. coverage 列出原文中每个需求短语（直接摘录原文）及其位置（"preference" 或轮次序号）。
输出格式：{"preference_session": [], "turns": [], "moved_constraint": null, "reference_turns": [], "coverage": [{"phrase": "", "where": 1}]}
原始需求：
"""


class Cover(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phrase: str
    where: Any


class Dialogue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    preference_session: list[str] = Field(default_factory=list)
    turns: list[str]
    moved_constraint: str | None = None
    reference_turns: list[int] = Field(default_factory=list)
    coverage: list[Cover] = Field(default_factory=list)


def check(d: Dialogue, original: str) -> list[str]:
    errors = []
    if not 2 <= len(d.turns) <= 3:
        errors.append("turns 必须为 2–3 轮")
    if len(d.preference_session) > 1:
        errors.append("preference_session 最多 1 句")
    if not d.reference_turns or not set(d.reference_turns) <= {2, 3} or max(d.reference_turns) > len(d.turns):
        errors.append("reference_turns 必须非空且只能是第 2、3 轮中存在的轮次")
    phrases = [c.phrase for c in d.coverage if c.phrase]
    if not phrases or sum(p in original for p in phrases) / len(phrases) < 0.8:
        errors.append("coverage 中至少 80% 的短语必须是原文摘录")
    # manual review (dev #8/#16): a habit sentence must not change the transport mode of the original
    for word, alts in (("地铁", ("地铁",)), ("打车", ("打车", "出租")), ("出租", ("打车", "出租")), ("步行", ("步行", "走路")),
                       ("火车", ("火车", "高铁", "动车")), ("高铁", ("火车", "高铁", "动车")), ("飞机", ("飞机", "航班"))):
        if any(word in p for p in d.preference_session) and not any(a in original for a in alts):
            errors.append(f"preference_session 中的“{word}”与原文的交通方式不一致")
    if len("".join(d.turns)) < 0.5 * len(original):
        errors.append("turns 总长度过短，可能遗漏了需求")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "test"], required=True)
    args = parser.parse_args()
    cfg = load_config(CONFIGS / "full.json")
    llm = build_client(cfg["llm"])
    llm.set_context(run_id=f"p4-mt-build-{args.split}", scope="p4")
    if args.split == "dev":
        mini = set(uid_list("dev_mini"))
        pool = [q for q in load_queries("medium") if q.uid not in mini]
        queries = random.Random(20261018).sample(pool, 60)
    else:
        queries = load_queries("human")

    def gen(q: Any) -> dict[str, Any] | None:
        messages = [{"role": "user", "content": PROMPT + q.text}]
        for attempt in (1, 2):
            try:
                d, _ = llm.chat_json(purpose="mt_build", messages=messages, schema=Dialogue,
                                     max_tokens=int(cfg["llm"]["max_tokens"]["mt_build"]))
            except (LLMOutputError, LLMRequestError):
                return None
            errors = check(d, q.text)
            if not errors:
                return {"uid": q.uid, "split": args.split, "preference_session": d.preference_session,
                        "turns": d.turns, "moved_constraint": d.moved_constraint,
                        "reference_turns": d.reference_turns, "gen_attempts": attempt}
            messages = [*messages, {"role": "assistant", "content": d.model_dump_json()},
                        {"role": "user", "content": "不符合要求：" + "；".join(errors) + "。请重新输出完整 json。"}]
        return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(gen, queries))
    kept = [r for r in rows if r]
    out = DATA / "mt" / f"{args.split}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
    stats = {"split": args.split, "source": len(queries), "kept": len(kept), "dropped": len(queries) - len(kept),
             "attempts": dict(Counter(r["gen_attempts"] for r in kept)),
             "with_preference": sum(bool(r["preference_session"]) for r in kept),
             "turns": dict(Counter(len(r["turns"]) for r in kept))}
    (DATA / "mt" / f"{args.split}.stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
