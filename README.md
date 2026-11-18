# 建筑巡检评级校准与升级复核

公房巡检的对象状态、评员评级、抽核和专业资格。
这里的记录全部为合成资料。日期按当地日历解释，未附时区的时间使用东八区。

## 交接资料

CSV 使用 UTF-8 和逗号分隔。值留空表示这条资料未给出该字段，不等于零。
`data/schema.json` 登记表头与跨表编号关系；`data/policy.json` 保存本期业务配置。

- `data/defects.csv`：同一对象维修前后状态不同；fixed 空则未确认维修。 字段：`defect`, `building`, `part`, `category`, `opened`, `fixed`, `verified_urgent`。
- `data/ratings.csv`：evidence 标识现场记录；reason 包括初评 initial、随机 random、争议 dispute、维修后维修确认 followup。 字段：`report`, `defect`, `inspector`, `date`, `evidence`, `grade`, `reason`, `blind`。
- `data/inspector_roles.csv`：专家标志仅说明可作专业锚点，不代表每份记录是无偏样本。 字段：`inspector`, `part`, `max_grade`, `expert`, `available_from`。
- `data/review_jobs.csv`：工单说明已经安排或完成的事项，不能替代原始评级。 字段：`job`, `defect`, `inspector`, `state`, `opened`, `deadline`。

## 资料校验

使用 Python 3.11.2 标准库，不需要安装扩展包。资料入口只核查交接格式，不计算业务结果。

```sh
python3 inspect_inputs.py
python3 -m unittest -v
```
