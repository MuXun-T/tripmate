# ChinaTravel 沙盒结构（勘查结果，T0.6）

> 来源：`third_party/ChinaTravel@f445e00`，沙盒 `LAMDA-NeSy/ChinaTravel-Sandbox@885c7da`（中文版，安装在 `chinatravel/environment/database/`）。以下样例取自上海。

## 规模（实测）

| 城市 | 景点 | 餐厅 | 酒店 |
|---|---|---|---|
| 北京 | 335 | 470 | 401 |
| 上海 | 360 | 484 | 403 |
| 南京 | 323 | 468 | 373 |
| 苏州 | 359 | 469 | 293 |
| 杭州 | 377 | 458 | 378 |
| 深圳 | 306 | 478 | 498 |
| 成都 | 333 | 467 | 379 |
| 武汉 | 334 | 457 | 368 |
| 广州 | 339 | 467 | 400 |
| 重庆 | 347 | 437 | 373 |
| 合计 | 3,413 | 4,655 | 3,866 |

论文给出的酒店数是 4,124，与当前沙盒（2026-08 数据修复后）不一致，以实测为准。

## 工具类与字段

- `Attractions().keys(city)`：id, name, type, lat, lon, opentime, endtime, price, recommendmintime, recommendmaxtime（时长单位：小时）。
  - 样例：`{'id': 0, 'name': '上海迪士尼度假区', 'type': '游乐园/体育娱乐', 'opentime': '08:30', 'endtime': '21:30', 'price': 399.0, 'recommendmintime': 2.0, 'recommendmaxtime': 3.5}`
  - 上海的景点类型：游乐园/体育娱乐、人文景观、文化旅游区、博物馆/纪念馆、商业街区、其它、历史古迹、公园、美术馆/艺术馆、红色景点、自然风光、大学校园（各城取值见 `env/enums.py` 运行结果）。
- `Restaurants().keys(city)`：id, name, lat, lon, price（人均）, cuisine, opentime, endtime, recommendedfood（逗号分隔，可能为空格）。
  - 菜系示例（上海）：素食、日本料理、江浙菜、粤菜、本帮菜、西餐、自助餐、川菜、小吃、融合菜、创意菜、面包甜点、咖啡店、海鲜、东南亚菜、酒吧/酒馆、清真菜、北京菜、火锅……
- `Accommodations().keys(city)`：id, name, hotelname_en, featurehoteltype, lat, lon, price（每间每晚）, numbed。
  - numbed 取值只有 1 和 2（上海：1→258，2→145）。计划中 `room_type` 必须等于该酒店的 numbed。
  - featurehoteltype 示例：免费停车、泳池、桑拿、管家服务、家庭房、停车场、机器人服务、SPA、设计师酒店、窗外好景、充电桩、江河景房、酒店公寓……（每家酒店只有一个值）。
- `IntercityTransport().select(start_city, end_city, "train"|"airplane", earliest_leave_time)`：
  - 火车列：TrainID, TrainType, From, To, BeginTime, EndTime, Duration（小时）, Cost（单张票价）。样例：`G101 北京南站→上海虹桥站 06:10–12:09 693.75`。
  - 航班列：FlightID, From, To, BeginTime, EndTime, Duration, Cost。样例：`FL081 北京首都国际机场→上海浦东国际机场 01:55–03:14 485.15`。
- `Transportation().goto(city, start, end, start_time, mode)` 返回分段列表，每段包含 start, end, mode, start_time, end_time, cost（**单人或单车价**）, distance：
  - walk：1 段，例如 `{'mode': 'walk', 'start_time': '10:00', 'end_time': '12:12', 'cost': 0.0, 'distance': 11.02}`；
  - metro：3 段，依次为步行到站、地铁、步行出站，例如 `迪士尼-地铁站 → 野生动物园-地铁站, cost 4`；
  - taxi：1 段，例如 `{'mode': 'taxi', 'cost': 44.28, 'distance': 11.02}`。
  - 车站和机场名称（如"上海虹桥站"）可以直接作为 goto 的起点或终点。
- `Poi().search(city, name)` 返回 `(lat, lon)`。
