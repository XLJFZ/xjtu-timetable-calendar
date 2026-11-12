# 按日期加课（`overrides.add_meetings`）

日期：2026-11-13
版本：0.11.0

## 背景

eHall 的主课表（`xskcb.do`）只给有调整的课程打 `ISTK=1` 标记，**不说明调到哪**；
具体的调课/补课记录在另一个接口 `xsdkkc.do`（`TKLX=01` 调课、`03` 补课）。
2026-11-13 实测：主课表与 10-14 的快照逐字段一致，但调课记录里有 4 场未来课程
不在主课表里，订阅产物因此缺课。

`docs/design-v0.1.md` §4.2 的缺口 C1 与 §7 的 D4(a) 已经选定了表达方式：
**扩展 `DateOverride`，增加 `add_meetings`**。本版把它实现出来。

## 语义

`overrides[日期].add_meetings` 是一个数组，每个元素表示**在该日额外上一次课**：

```json
"2026-11-14": {
  "add_meetings": [
    {
      "course_id": "COMP401727",
      "periods": [5, 6, 7, 8],
      "location": "3-4007",
      "note": "补课（教务调课记录，申请 2026-11-06）"
    }
  ]
}
```

- `course_id` / `course_name`：引用既有的课程安排，至少给一个；同时给时以
  `course_id` 为准。课程身份（`stable_course_key`）沿用被引用的那条安排。
- `periods`：必填，本次实际上的节次；钟点按**该日期当天适用的作息**解析。
- `location` / `teacher`：可选，覆盖本次的教室/教师；不填沿用被引用课程。
- `note`：可选，追加到 `DESCRIPTION`。

与 `source_date` 的区别：`source_date` 是「整日换成另一天的课表」，
`add_meetings` **只新增指定的课，不动该日其它课**。

UID 与普通事件同一配方（`semester | course_key | day | periods`），因此稳定可复现；
同一门课在别处的正常排课不会被它影响。

## 失败模式（fail-closed）

以下情况在配置加载或导出时直接报错，绝不静默跳过：

- 同一条 override 同时声明 `cancel: true` 与 `add_meetings`；
- 加课日落在 `excluded_dates`（全校停课日）；
- 加课日早于第 1 教学周或超出 `total_weeks`；
- 引用的课程在课表里找不到；
- 同名/同课程号有多条安排且 `periods` 无法唯一匹配（不替用户猜）。

## 影响面

- 没有 `add_meetings` 的配置：解析路径与产物字节不变。
- 自动获取 `xsdkkc.do` **不在本版范围**：本版只提供配置口径，调课记录仍需手工
  抄进学期配置（或由调用方自行生成）。是否把「抓取调课记录」并入 `fetch`，
  另开设计决策。
