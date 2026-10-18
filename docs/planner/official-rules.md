# 官方校验规则勘查（T0.6）

> 源码：`chinatravel/symbol_verification/commonsense_constraint.py`（下称 CC）、`concept_func.py`、`hard_constraint.py`、`chinatravel/evaluation/*.py`，commit f445e00。列名照抄原文，原文拼写错误一并保留。

## 1. 常识检查（8 个函数）

常识检查只用到 query 的 `start_city`、`target_city`、`people_number` 三个字段（CC 中所有 `symbolic_input[...]` 的访问），**不涉及 oracle 字段**。

### Is_activity_grounded（CC:92）
- 列：`Invalid or ungrounded activity entity`。
- 计划必须是 dict 并且有非空的 `itinerary`；否则报 "Error plan type, must be python dict" 或 "Error plan type, must provide itinerary"。
- train/airplane 只能是**全程第一个或最后一个**活动；不能带 position；必须有 start、end；airplane 必须有 FlightID，train 必须有 TrainID，且不能出现另一种 ID。
- 其余活动类型只能是 attraction / breakfast / lunch / dinner / accommodation，必须有 position，且不能带 start、end、TrainID、FlightID。

### Is_intercity_transport_correct（CC:180）
- 列：`Intercity transportation events must occur`、`Invalid Trains or Airplanes, given TrainID/FlightID, origin and destination`、`Incorrect Information of Intercity Transport on price or duration`、`Incorrect Cost on Intercity Transportation`。
- 第一天的第一个活动是去程，最后一天的最后一个活动是返程；必须带 FlightID 或 TrainID，否则报 "It must contain intecity transport" 或 "The first activity should be an intercity transport."。
- 类型必须是 airplane 或 train（"Intercity transport type should be airplane or train"）。
- 去程用 `select(start_city, target_city, type)` 匹配，返程用 `select(target_city, start_city, type)` 匹配：ID、start==From、end==To 都要一致。
- `start_time == BeginTime` 且 `end_time == EndTime`；`price == Cost`。
- 必须有 `tickets` 和 `cost`，且满足 `|price × tickets − cost| ≤ 0.1`。

### Is_attractions_correct（CC:466）
- 列：`Unavailable attractions`、`Visiting attraction in their closed time`、`Repeated attraction Choices`、`Incorrect price Information of attraction`、`Incorrect cost Information of attraction`。
- position 必须是 target_city 的景点名；否则报 "No information found given attraction"，并且 5 列全部置 1。
- 必须满足 `opentime ≤ start_time` 且 `end_time ≤ endtime`；endtime ≤ opentime 时把 endtime 加 24 小时。
- `int(price) == int(沙盒 price)`，否则报 "Attraction price should be provided" 一类错误；`|price × tickets − cost| ≤ 0.1`。
- 全程不得重复同一景点。

### Is_hotels_correct（CC:575）
- 列：`Unavailable Accommodation`、`Incorrect Information of Accommodation on price or room type`、`Incorrect cost Information of Accommodation`、`Accomondation is necessary for trips longer than one day`。
- 名称必须存在；`price == 沙盒 price`（"Hotel price should be provided"）；`room_type == numbed`（"Room information should be provided"）。
- `|rooms × price − cost| ≤ 0.1`（"Cost and rooms information should be provided"）。
- 天数 > 1 时至少要有一个 accommodation 活动（"We need a hotel for a trip more than one day."）。
- 房间数与人数的关系**不检查**。

### Is_restaurants_correct（CC:672）
- 列：`Unavailable Restruants`、`Visiting Restruants in their closed time`、`Repeated Restruants Choices`、`Incorrect price Information of Restruants`、`Incorrect cost Information of Restruants`、`Inappropriate Meal Times`、`Repeated Meal Types in One Day`。
- 每天 breakfast、lunch、dinner 各最多 1 次。
- **早餐**：position 可以是酒店名，此时 `price` 必须为 0（"Have breakfast at hotel, price 0"），且不得满足 `start_time ≥ 09:00` 或 `end_time ≤ 06:00`（"The time of breakfast should be provided" / 窗口 [06:00 -- 09:00]）。
- **午餐**：不得满足 `start_time ≥ 14:00` 或 `end_time ≤ 11:00`；**晚餐**：不得满足 `start_time ≥ 20:00` 或 `end_time ≤ 17:00`（"Schedule of Restruants should be provided"）。
- `price == 沙盒 price`（"price of Restruants should be provided"）；`|people_number × price − cost| ≤ 0.1`（"The Restruants Events should provide cost information"）。
- 必须在营业时间内：opentime ≤ start 且 end ≤ endtime（跨夜规则同景点）。
- 全程不得重复同一餐厅（酒店早餐不计入）。

### Is_transport_correct（CC:832）
- 列：`Unavailable Inner-City Transport`、`Incorrect Information of Inner-City Transporton on price, distance, and duration`、`Inccorrect cost information of Inner-City Transport`。代码里实际写入的是 `Incorrect cost information of Inner-City Transport` 和 `Incorrect Duration Information of Inner-City Transport`，两个拼写都会出现。
- 每个非空的 `transports` 都会用 `goto(target_city, transports[0].start, transports[-1].end, transports[0].start_time, mode)` 重新计算并逐段比对：start、end、start_time、end_time 必须一致；`|price − 工具 cost| ≤ 0.1`；`|distance − 工具 distance| ≤ 0.1`。
- 3 段 = 地铁，必须依次是 walk、metro、walk；metro 段需要 `tickets`，且 `cost = price × tickets`；walk 段 cost 必须为 0。
- 1 段 = walk（cost 0）或 taxi（需要 `cars`，且 `cost = price × cars`）。
- 其他段数报 "Metro transport should be three-stages, Taxi or walk should be one-stage."。
- **结论**：transports 直接复制 goto 返回的分段，并做以下映射：`price = 工具 cost`；metro 段 `tickets = 人数`、`cost = price × 人数`；taxi 段 `cars = 车辆数`、`cost = price × 车辆数`；walk 段 `price = cost = 0`；`distance` 原样复制。

### Is_time_correct（CC:1057）
- 列：`Invalid duration information of each activity`、`Does not follow Chronological Order`。
- 每个活动都必须有 start_time 和 end_time；非城际交通活动必须满足 start < end。
- **同一天内**：活动开始时间 ≥ 上一个活动结束时间；transports[0].start_time ≥ 上一个活动结束时间；活动开始时间 ≥ transports[-1].end_time。前一活动的结束时间在每天开始时重置。
- time2real 会去掉"次日"前缀；"24:00" 解析为 1440。

### Is_space_correct（CC:1134）
- 列：`Invalid Transport information across positions`。
- **位置链跨天连续**（position_list 不按天重置）：城际交通的当前位置取 start，之后把 end 计入位置链；其他活动取 position。
- 当前位置与上一个位置不同时，transports 必须非空，且 `transports[0].start == 上一个位置`、`transports[-1].end == 当前位置`。
- 位置相同时 transports 必须为空列表（例如在酒店吃早餐）。
- 每个活动都必须有 `transports` 键（可以为空列表）。
- train/airplane 只能是全程第一个或最后一个活动。

## 2. 默认约束（dev 450 条全部出现，模板原文）

```
result=(day_count(plan)==<days>)
result=(people_count(plan)==<people>)
result=True
for activity in allactivities(plan):
  if activity_type(activity) in ['attraction', 'airplane', 'train'] and activity_tickets(activity)!=<people>: result=False
  if innercity_transport_type(activity_transports(activity))=='metro'and metro_tickets(activity_transports(activity))!=<people>: result=False
result=True
for activity in allactivities(plan):
  if innercity_transport_type(activity_transports(activity))=='taxi'and taxi_cars(activity_transports(activity))!=<cars>: result=False
```

实测（dev）：tickets 恒等于人数；cars 在人数 1–4 时都是 1，因此规划时按 `cars = ceil(人数 / 4)` 取值。

## 3. 其他约束模板（dev 中出现次数）

| 次数 | 含义 | 模板关键片段 | 示例 uid |
|---|---|---|---|
| 208 | 总预算 | `total_cost += activity_cost + innercity_transport_cost ... result=(total_cost<=X)` | e20241028160248698752 |
| 179 | 房间数 | `activity_type=='accommodation' and room_count(activity)!=N` | h20241204223651527057 |
| 150 | 房型（每间床数） | `room_type(activity)!=N` | h20241204223651527057 |
| 136 | 城际交通方式 | `result=({'train'}==intercity_transport_set)` | t20241206164116321945 |
| 71 | 必去景点 | `{'人民大礼堂'}<=attraction_name_set` | m20241028164822843097 |
| 63 | 菜系 | `{'本帮菜'}<=restaurant_type_set` | m20241028164637595271 |
| 61 | 景点类型 | `{'自然风光'}<=attraction_type_set` | m20241028164620332052 |
| 51 | 酒店特色 | `{'儿童乐园'}<=accommodation_type_set` | h20241204223651527057 |
| 31 | 必去餐厅 | `{...}<=restaurant_name_set` | m20241028164905266860 |
| 30 | 指定酒店 | `{...}<=accommodation_name_set` | m20241028164947135130 |
| 28 | 只用某种市内交通 | `innercity_transport_set<={'metro'}` | t20241206171807314229 |
| 20 | 至少用过某种市内交通 | `{'metro'}<=innercity_transport_set` | e20241028160842228543 |
| 13 | 人均餐费上限 | `food_cost/food_count/people_count(plan)<=X` | e20241028161340383778 |
| 1 | 人均每晚住宿上限 | `hotel_cost/people_count(plan)/(day_count(plan)-1)<= X` | e20241028160857444675 |
| 1 | 用过多种市内交通 | `{'metro','walk'}<=innercity_transport_set` | e20241028161355587050 |

注意：总预算模板中 `activity_cost` 已经包含城际交通、景点、餐饮和住宿费用，再加上所有 transports 的 cost。

## 4. 硬约束执行器

- 单个计划：`chinatravel.symbol_verification.hard_constraint.evaluate_constraints_py(hard_logic_py: list[str], plan: dict, verbose=False) -> list[bool]`。每条 DSL 用 `execute_dsl_code` 在 `func_dict`（concept_func 中的函数）里执行，读取变量 `result`。
- 批量：`chinatravel.evaluation.hard_constraint.evaluate_hard_constraints_v2(data_index, symbolic_input_dict, plan_json_dict, env_pass_id, verbose, lang)`。
- 推理时用我们编译出的 DSL 列表调用单计划接口，不读 oracle。

## 5. FPR 的计算

`eval_exp.py`：`all_pass_id = schema_pass_id ∩ commonsense_pass_id ∩ logi_pass_id`，`FPR = |all_pass_id| / N × 100`。其中：
- DR = `evaluate_schema_constraints` 的通过率（jsonschema 校验 output_schema.json）；
- EPR micro/macro 来自 `evaluate_commonsense_constraints`；
- LPR micro/macro 与 C-LPR（conditional micro）来自 `evaluate_hard_constraints_v2`。
