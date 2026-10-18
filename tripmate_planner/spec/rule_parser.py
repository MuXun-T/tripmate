"""Deterministic rule-based extraction of TripSpec fields from Chinese text (T1.2)."""

import re

from tripmate_planner.env.enums import Enums
from tripmate_planner.env.sandbox import CITIES
from tripmate_planner.spec.tripspec import TripSpec

CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
NUM = r"(\d+|[一二两三四五六七八九十])"
HUMAN_PREFIX = re.compile(
    r"\[当前位置(?P<start>[^,，\]]+)[,，]目标位置(?P<target>[^,，\]]+)[,，]旅行人数(?P<people>\d+)[,，]旅行天数(?P<days>\d+)\]")
COMPANION = r"(朋友|女朋友|男朋友|家人|老婆|老公|妻子|丈夫|对象|爱人|同事|同学|闺蜜|父母|爸妈|孩子|儿子|女儿)"


def _num(token: str) -> int:
    return int(token) if token.isdigit() else CN[token]


def _city_after(text: str, pattern: str) -> str | None:
    m = re.search(pattern + r"(" + "|".join(CITIES) + ")", text)
    return m.group(1) if m else None


def _people(text: str) -> int | None:
    m = re.search(r"我和" + NUM + r"(?:位|个)" + COMPANION, text)
    if m:
        return _num(m.group(1)) + 1
    m = re.search(NUM + r"(?:个人|人|位)(?!民币)", text)
    if m:
        return _num(m.group(1))
    if re.search(r"我一个人|独自|一个人", text):
        return 1
    if re.search(r"我和" + COMPANION, text):
        return 2
    return None


def _longest_names(text: str, names: tuple[str, ...]) -> list[str]:
    found = sorted({n for n in names if len(n) >= 3 and n in text}, key=len, reverse=True)
    kept: list[str] = []
    for name in found:
        if not any(name in longer for longer in kept):
            kept.append(name)
    return kept


def _enum_hits(text: str, values: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for value in sorted(values, key=len, reverse=True):
        parts = [p for p in value.split("/") if len(p) >= 2 and p not in {"其它", "其他"}]
        if any(p in text for p in parts) and not any(value in h for h in hits):
            hits.append(value)
    return hits


def parse_rules(text: str, enums: Enums) -> TripSpec:
    data: dict[str, object] = {}
    m = HUMAN_PREFIX.search(text)
    if m and m.group("start") in CITIES and m.group("target") in CITIES:
        data.update(start_city=m.group("start"), target_city=m.group("target"),
                    people_number=int(m.group("people")), days=int(m.group("days")))
    else:
        start = _city_after(text, r"当前位置")
        if start:
            data["start_city"] = start
        target = _city_after(text, r"(?:去|到|前往|游玩)")
        if target and target != start:
            data["target_city"] = target
        dm = re.search(NUM + r"天", text)
        if dm:
            data["days"] = _num(dm.group(1))
        people = _people(text)
        if people:
            data["people_number"] = people
    bm = re.search(r"预算(?:为|是|大约|约|在|控制在)?\s*(\d+(?:\.\d+)?)", text) or re.search(r"(\d+)\s*元以内", text)
    if bm:
        data["budget_total"] = float(bm.group(1))
    mm = re.search(r"(?:人均每顿饭|每顿饭人均|人均每餐|每餐人均)(?:不超过|不要超过|控制在|低于)?\s*(\d+)", text)
    if mm:
        data["meal_price_max_per_person"] = float(mm.group(1))
    train, plane = re.search(r"火车|高铁|动车", text), re.search(r"飞机|航班", text)
    if train and not plane:
        data["intercity_go"] = data["intercity_back"] = "train"
    elif plane and not train:
        data["intercity_go"] = data["intercity_back"] = "airplane"
    if re.search(r"地铁", text):
        data["innercity_mode"] = "metro"
    elif re.search(r"打车|出租车|的士", text):
        data["innercity_mode"] = "taxi"
    elif re.search(r"步行|走路", text):
        data["innercity_mode"] = "walk"
    rm = re.search(r"([一两二三四五1-5])间", text)
    if rm:
        data["rooms"] = _num(rm.group(1))
    if re.search(r"大床|单床", text):
        data["room_type"] = 1
    elif re.search(r"双床|标间", text):
        data["room_type"] = 2
    city = data.get("target_city")
    if isinstance(city, str):
        data["attraction_types"] = _enum_hits(text, enums.attraction_types[city])
        data["cuisines"] = _enum_hits(text, enums.cuisines[city])
        data["hotel_features"] = _enum_hits(text, enums.hotel_features[city])
        data["must_visit"] = _longest_names(text, enums.names[(city, "attraction")])
        data["must_eat_at"] = _longest_names(text, enums.names[(city, "restaurant")])
        hotels = _longest_names(text, enums.names[(city, "hotel")])
        if hotels:
            data["must_stay_at"] = hotels[0]
    spec = TripSpec.model_validate(data)
    spec.field_sources = {k: "rule" for k, v in data.items() if v not in (None, [], "")}
    return spec
