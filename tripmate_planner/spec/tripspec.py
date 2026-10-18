"""TripSpec: structured trip constraints compiled from natural language (appendix A)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

City = Literal["北京", "上海", "南京", "苏州", "杭州", "深圳", "成都", "武汉", "广州", "重庆"]
FieldSource = Literal["rule", "llm", "memory", "default"]
LIST_FIELDS = ("must_visit", "must_eat_at", "attraction_types", "cuisines", "avoid_cuisines", "foods",
               "hotel_features", "notes")


class TripSpecCore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_city: City | None = None
    target_city: City | None = None
    days: int | None = Field(default=None, ge=1, le=7)
    people_number: int | None = Field(default=None, ge=1, le=10)
    budget_total: float | None = Field(default=None, gt=0)
    # 偏差（T1.2 记录）：dev oracle 中有 13 条"人均每顿饭不超过 X 元"约束，附录 A 未列，补充此字段
    meal_price_max_per_person: float | None = Field(default=None, gt=0)
    intercity_go: Literal["train", "airplane"] | None = None
    intercity_back: Literal["train", "airplane"] | None = None
    innercity_mode: Literal["metro", "taxi", "walk"] | None = None
    rooms: int | None = Field(default=None, ge=1, le=10)
    room_type: Literal[1, 2] | None = None
    must_visit: list[str] = Field(default_factory=list, max_length=10)
    must_eat_at: list[str] = Field(default_factory=list, max_length=10)
    must_stay_at: str | None = None
    attraction_types: list[str] = Field(default_factory=list, max_length=10)
    cuisines: list[str] = Field(default_factory=list, max_length=10)
    avoid_cuisines: list[str] = Field(default_factory=list, max_length=10)
    foods: list[str] = Field(default_factory=list, max_length=10)
    hotel_features: list[str] = Field(default_factory=list, max_length=5)
    pace: Literal["relaxed", "normal", "intense"] = "normal"
    notes: list[str] = Field(default_factory=list, max_length=5)


class TripSpec(TripSpecCore):
    field_sources: dict[str, FieldSource] = Field(default_factory=dict)

    def core(self) -> TripSpecCore:
        return TripSpecCore.model_validate(self.model_dump(exclude={"field_sources"}))

    def is_plannable(self) -> bool:
        return None not in (self.start_city, self.target_city, self.days, self.people_number)
