import sqlite3
import time

import pytest

from tripmate_planner.memory.store import MemoryStore, now_iso
from tripmate_planner.spec.memory_merge import apply_preferences
from tripmate_planner.spec.tripspec import TripSpec


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "m.sqlite3")


def test_add_and_supersede(store) -> None:
    a = store.add_preference("u", "innercity_mode", "metro", "平时坐地铁")  # 1 新增
    b = store.add_preference("u", "innercity_mode", "taxi", "以后打车")  # 2 覆盖
    hist = store.preference_history("u", "innercity_mode")
    assert hist[0]["pref_id"] == a and hist[0]["valid_to"] and hist[0]["superseded_by"] == b
    assert store.active_preferences("u")[0]["value"] == "taxi"


def test_same_value_no_new_row(store) -> None:  # 3
    a = store.add_preference("u", "pace", "relaxed", "慢一点")
    assert store.add_preference("u", "pace", "relaxed", "还是慢一点") == a
    assert len(store.preference_history("u", "pace")) == 1


def test_delete_and_history_and_readd(store) -> None:
    store.add_preference("u", "room_type", 2, "双床")
    assert store.delete_preference("u", "room_type")  # 4 删除后不可见
    assert store.active_preferences("u") == []
    assert len(store.preference_history("u", "room_type")) == 1  # 5 历史保留
    store.add_preference("u", "room_type", 1, "大床")  # 6 删除后再新增
    assert store.active_preferences("u")[0]["value"] == 1


def test_as_of_queries(store) -> None:
    store.add_preference("u", "innercity_mode", "metro", "e")
    time.sleep(0.01)
    t1 = now_iso()
    time.sleep(0.01)
    store.add_preference("u", "innercity_mode", "taxi", "e")
    assert store.active_preferences("u", as_of=t1)[0]["value"] == "metro"  # 7 覆盖前
    store.add_preference("u", "pace", "intense", "e")
    time.sleep(0.01)
    t2 = now_iso()
    time.sleep(0.01)
    store.delete_preference("u", "pace")
    assert any(p["key"] == "pace" for p in store.active_preferences("u", as_of=t2))  # 8 删除前


def test_user_isolation(store) -> None:  # 9
    store.add_preference("u1", "pace", "relaxed", "e")
    assert store.active_preferences("u2") == []


def test_invalid_key_and_value(store) -> None:
    with pytest.raises(ValueError):
        store.add_preference("u", "hotel_star", 5, "e")  # 10
    with pytest.raises(ValueError):
        store.add_preference("u", "innercity_mode", "rocket", "e")  # 11


def test_recent_turns_truncation(store) -> None:
    for i in range(1, 10):
        store.append_turn("c", i, "u", "user", f"第{i}轮" + "长" * 50)
    assert len(store.recent_turns("c", "u", max_turns=3, max_chars=10_000)) == 3  # 12 轮数
    assert len(store.recent_turns("c", "u", max_turns=9, max_chars=120)) == 2  # 13 字符


def test_latest_state(store) -> None:  # 14
    store.save_state("c", 1, "u", {"days": 1})
    store.save_state("c", 3, "u", {"days": 3})
    store.save_state("c", 2, "u", {"days": 2})
    assert store.latest_state("c", "u") == (3, {"days": 3})


def test_unique_active_index(store) -> None:  # 15
    store.add_preference("u", "pace", "relaxed", "e")
    with pytest.raises(sqlite3.IntegrityError), store._conn() as c:
        c.execute("INSERT INTO user_preference VALUES ('x','u','pace','\"intense\"','explicit_confirmed','e',?,NULL,NULL,NULL)",
                  (now_iso(),))


def test_merge_priority_and_temporary_override() -> None:
    prefs = [{"key": "innercity_mode", "value": "metro"}, {"key": "cuisines_prefer", "value": ["川菜"]},
             {"key": "room_type", "value": 2}]
    explicit = TripSpec(innercity_mode="taxi", field_sources={"innercity_mode": "llm"})
    spec, applied, hints = apply_preferences(explicit, prefs)
    assert spec.innercity_mode == "taxi"  # this trip's explicit request wins, long-term untouched
    assert spec.room_type == 2 and spec.field_sources["room_type"] == "memory"
    assert hints == ["川菜"] and spec.cuisines == []  # soft hint only
    spec2, _, _ = apply_preferences(TripSpec(), prefs)
    assert spec2.innercity_mode == "metro"
