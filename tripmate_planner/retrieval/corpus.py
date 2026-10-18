"""POI corpus: one sandbox record = one document (structured records are not window-chunked)."""

import hashlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from tripmate_planner.env.sandbox import CITIES, get_sandbox

KINDS = ("attraction", "restaurant", "hotel")


@dataclass(frozen=True)
class PoiDoc:
    doc_id: str
    city: str
    kind: str
    name: str
    text: str
    meta: dict[str, Any] = field(hash=False, compare=False)


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _text(city: str, kind: str, row: dict[str, Any]) -> str:
    if kind == "attraction":
        return (f"{row['name']}｜{city}｜景点｜类型:{row['type']}｜门票:{_fmt(row['price'])}元｜"
                f"建议游玩:{_fmt(row['recommendmintime'])}-{_fmt(row['recommendmaxtime'])}小时")
    if kind == "restaurant":
        return (f"{row['name']}｜{city}｜餐厅｜菜系:{row['cuisine']}｜推荐菜:{_fmt(row['recommendedfood'])}｜"
                f"人均:{_fmt(row['price'])}元")
    return (f"{row['name']}｜{city}｜酒店｜特色:{row['featurehoteltype']}｜床数:{_fmt(row['numbed'])}｜"
            f"价格:{_fmt(row['price'])}元/晚")


@lru_cache(maxsize=1)
def build_corpus() -> tuple[PoiDoc, ...]:
    sb = get_sandbox()
    docs: list[PoiDoc] = []
    for city in CITIES:
        for kind in KINDS:
            for row in sb.table(city, kind).to_dict("records"):
                docs.append(PoiDoc(doc_id=f"{city}:{kind}:{int(row['id'])}", city=city, kind=kind,
                                   name=str(row["name"]), text=_text(city, kind, row), meta=row))
    return tuple(docs)


def corpus_hash(docs: tuple[PoiDoc, ...]) -> str:
    joined = "\n".join(sorted(f"{d.doc_id}\t{d.text}" for d in docs))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
