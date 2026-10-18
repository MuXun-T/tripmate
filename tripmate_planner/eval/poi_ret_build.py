"""T2.6: build POI-Ret items from oracle constraints (attraction/restaurant/hotel type and named POIs)."""

import json
from collections import Counter

from tripmate_planner.data.splits import uid_list
from tripmate_planner.eval.labels import gold_from_record
from tripmate_planner.eval.oracle import load_oracle_records
from tripmate_planner.paths import DATA
from tripmate_planner.retrieval.corpus import build_corpus

FIELD_MAP = {"attraction_types": ("attraction", "attraction_type", "type"),
             "cuisines": ("restaurant", "restaurant_type", "cuisine"),
             "hotel_features": ("hotel", "accommodation_type", "featurehoteltype"),
             "must_visit": ("attraction", "position", "name"),
             "must_eat_at": ("restaurant", "position", "name"),
             "must_stay_at": ("hotel", "position", "name")}


def build(split: str) -> tuple[list[dict], Counter]:
    docs = build_corpus()
    uids = uid_list(split)
    records = load_oracle_records(uids)
    items, dropped = [], Counter()
    for uid in uids:
        rec = records[uid]
        labels, _, _ = gold_from_record(rec)
        city = rec["target_city"]
        for lab in sorted(labels, key=lambda x: (x.field, str(x.value))):
            if lab.field not in FIELD_MAP:
                continue
            kind, ctype, col = FIELD_MAP[lab.field]
            rel = [d.doc_id for d in docs if d.city == city and d.kind == kind
                   and (d.name if col == "name" else d.meta.get(col)) == lab.value]
            if not rel:
                dropped[ctype] += 1
                continue
            items.append({"item_id": f"{uid}:{ctype}:{lab.value}", "uid": uid, "split": split, "kind": kind,
                          "ctype": ctype, "value": lab.value, "query_text": rec["nature_language"], "city": city,
                          "relevant_doc_ids": rel})
    return items, dropped


def main() -> None:
    for name in ("dev", "test"):
        items, dropped = build(name)
        out = DATA / "poi_ret" / f"{name}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
        print(name, len(items), dict(Counter(i["ctype"] for i in items)), "dropped:", dict(dropped))


if __name__ == "__main__":
    main()
