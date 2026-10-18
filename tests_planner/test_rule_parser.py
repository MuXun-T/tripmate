import pytest

from tripmate_planner.env.enums import get_enums
from tripmate_planner.spec.rule_parser import parse_rules

CASES = [
    ("当前位置北京。我一个人想去上海玩一天，请给我一个旅行规划。", {"start_city": "北京", "target_city": "上海", "days": 1, "people_number": 1}),
    ("当前位置南京。我和朋友想去杭州玩两天。", {"target_city": "杭州", "days": 2, "people_number": 2}),
    ("当前位置广州。我和三位同事去深圳玩3天。", {"people_number": 4, "days": 3}),
    ("当前位置成都。我们四个人想去重庆玩2天，预算5000元。", {"people_number": 4, "budget_total": 5000.0}),
    ("当前位置武汉。我想去北京玩三天，预算控制在 3000 人民币。", {"budget_total": 3000.0, "days": 3}),
    ("当前位置苏州。两个人去南京玩2天，坐高铁往返。", {"intercity_go": "train", "intercity_back": "train", "people_number": 2}),
    ("当前位置上海。我想去成都玩3天，选择飞机出行。", {"intercity_go": "airplane", "intercity_back": "airplane"}),
    ("当前位置杭州。我一个人去上海玩2天，市内交通只坐地铁。", {"innercity_mode": "metro"}),
    ("当前位置北京。我想去天津？不，去苏州玩2天，全程打车。", {"innercity_mode": "taxi", "target_city": "苏州"}),
    ("当前位置深圳。我想去广州玩一天，喜欢步行。", {"innercity_mode": "walk"}),
    ("当前位置南京。我们三个人去上海玩2天，开两间双床房。", {"rooms": 2, "room_type": 2}),
    ("当前位置重庆。我两个人去成都玩2天，住一间大床房。", {"rooms": 1, "room_type": 1}),
    ("当前位置北京。我想去上海玩2天，喜欢博物馆。", {"attraction_types": ["博物馆/纪念馆"]}),
    ("当前位置北京。我想去上海玩2天，想吃本帮菜和火锅。", {"cuisines": ["本帮菜", "火锅"]}),
    ("当前位置北京。我想去上海玩2天，酒店要有泳池。", {"hotel_features": ["泳池"]}),
    ("当前位置北京。我想去上海玩2天，想去东方明珠。", {"must_visit": ["东方明珠"]}),
    ("当前位置北京。我想去上海玩2天，人均每顿饭不超过80元。", {"meal_price_max_per_person": 80.0}),
    ("[当前位置上海,目标位置苏州,旅行人数2,旅行天数3] 我要带着我大儿子去苏州玩三天。", {"start_city": "上海", "target_city": "苏州", "people_number": 2, "days": 3}),
    ("当前位置成都。我和两位朋友打算去武汉玩两天。", {"people_number": 3, "days": 2}),
    ("当前位置杭州。我独自去北京玩5天。", {"people_number": 1, "days": 5}),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_rule_parser(text: str, expected: dict) -> None:
    spec = parse_rules(text, get_enums())
    for key, value in expected.items():
        got = getattr(spec, key)
        if isinstance(value, list):
            assert sorted(got) == sorted(value), (key, got)
        else:
            assert got == value, (key, got)
