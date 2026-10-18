# P1 约束抽取评测（dev，450 条）

> 由 `python -m tripmate_planner.eval.extraction_eval --split dev --config configs/planner/full.json [--mode rule]` 生成。标签由 oracle 的 hard_logic_py 模板解析得到，只用于评测；默认约束（票数、出租车车辆数）不计入。

- 覆盖率（非默认约束中可解析为标签的比例）：0.9990，未覆盖 2 条

| 配置 | micro P | micro R | micro F1 | 精确匹配率 | P50 延迟 ms | P95 延迟 ms | 平均 prompt token | 平均输出 token | 前缀缓存命中占比 | 费用（高峰价，元） | run_id |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rule | 0.9659 | 0.9346 | 0.95 | 0.7556 | 0.3 | 4.2 | 0.0 | 0.0 | 0.0 | 0 | p1-extract-dev-rule-v2 |
| llm+rule | 0.9865 | 0.9815 | 0.984 | 0.9089 | 984.3 | 1918.1 | 1221.7 | 107.6 | 0.7935 | 0.5385 | p1-extract-dev-llm |

## 字段级 F1

| 字段 | rule F1 | llm+rule F1 | llm+rule TP/FP/FN |
|---|---|---|---|
| attraction_types | 0.8 | 0.8841 | 61/16/0 |
| budget_total | 0.9976 | 1.0 | 208/0/0 |
| cuisines | 0.843 | 0.9844 | 63/2/0 |
| days | 1.0 | 1.0 | 450/0/0 |
| hotel_features | 0.7381 | 0.9903 | 51/1/0 |
| innercity_mode | 0.9495 | 0.9897 | 48/1/0 |
| intercity | 1.0 | 1.0 | 136/0/0 |
| meal_price_max_per_person | 0.5556 | 1.0 | 13/0/0 |
| must_eat_at | 0.6809 | 0.7347 | 18/0/13 |
| must_stay_at | 0.8148 | 0.8462 | 22/0/8 |
| must_visit | 0.8811 | 0.9565 | 66/1/5 |
| people_number | 0.9444 | 0.9978 | 449/1/1 |
| room_type | 0.9553 | 0.9799 | 146/2/4 |
| rooms | 0.9803 | 0.9803 | 174/2/5 |

## 未覆盖的约束模板（前 10）

- 1 次：`result=(hotel_cost/people_count(plan)/(day_count(plan)-1)<= 500)`
- 1 次：`result=({'metro','walk'}<=innercity_transport_set)`

## 说明

- dev（easy+medium）是模板生成的查询，规则解析本身就很强；LLM 的增益主要来自规则覆盖不到的说法（例如“当地特色菜→本帮菜”、往返方式省略）。human 集的口语化表达只能在 T5 测试时观察。
- llm+rule 有 1 条因输出校验失败退回纯规则（notes 字段被模型输出为字符串）。
- 偏差：TripSpec 增加了 `meal_price_max_per_person` 字段（dev 中有 13 条人均餐费约束，附录 A 未列）；只给出单程交通方式时，另一程沿用相同方式（数据集约定）。
