import pytest
from pydantic import ValidationError

from tripmate_planner.env.enums import get_enums
from tripmate_planner.spec.normalize import normalize_spec
from tripmate_planner.spec.tripspec import TripSpec


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TripSpec.model_validate({"target_city": "上海", "hotel_star": 5})
    with pytest.raises(ValidationError):
        TripSpec.model_validate({"target_city": "火星"})


def test_normalize_enums_and_names() -> None:
    enums = get_enums()
    spec = TripSpec(target_city="上海", attraction_types=["博物馆/纪念馆", "外星基地"],
                    cuisines=["本帮菜", "本帮菜"], must_visit=["上海迪士尼度假区", "东方明珠塔"],
                    must_eat_at=["根本不存在的餐厅XYZ"])
    out, issues = normalize_spec(spec, enums)
    assert out.attraction_types == ["博物馆/纪念馆"]
    assert out.cuisines == ["本帮菜"]
    assert "上海迪士尼度假区" in out.must_visit
    assert "东方明珠" in out.must_visit  # fuzzy match to exact sandbox name
    assert out.must_eat_at == []
    assert any("外星基地" in i for i in issues) and any("XYZ" in i for i in issues)


def test_names_dropped_without_city() -> None:
    out, issues = normalize_spec(TripSpec(must_visit=["东方明珠"]), get_enums())
    assert out.must_visit == [] and issues
