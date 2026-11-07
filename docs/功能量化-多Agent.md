# 多 Agent 分工（2026-11-07）

`multi` 在开放写入或命令后由三个子代理分工。Planner 只出带依赖的计划，不调用工具。Worker 只执行分到的步骤，看不到其他 Worker 的原始消息。Reviewer 可以读取并运行验证命令，不能编辑。验证不通过就打回，最多 2 次；第 3 次仍不通过就停止，不再执行新步骤。

下面的数字来自种子 `20261107` 的 12 次离线假模型试验。每次试验事先抽一个失败预算 0、1、2 或 3：预算用完前审查者一律不通过，预算为 3 时始终不通过。

```sh
python -X utf8 -c "import json; from tests.core.test_subagents import measure_team; print(json.dumps(measure_team(), ensure_ascii=False))"
```

实测输出：

```json
{
  "seed": 20261107,
  "trials": 12,
  "passed": 10,
  "sendbacks": {"2": 4, "0": 3, "1": 5},
  "isolated_tokens": 1136,
  "combined_tokens": 4192
}
```

| 项目 | 数值 |
| --- | --- |
| 试验次数 | 12 |
| 终态 returned | 10 |
| 通过率 | 10/12 |
| 打回 0 次 | 3 轮 |
| 打回 1 次 | 5 轮 |
| 打回 2 次 | 4 轮 |
| 打回超过 2 次 | 0 |
| 审查者请求的 token（隔离后） | 1136 |
| 把该次 Worker 原文并进审查请求后的 token | 4192 |
| 隔离后少掉的比例 | (4192 − 1136) / 4192 = 3056/4192 |

token 用引擎的 `json_tokens` 估算，取这 12 次里第一次试验的审查请求。隔离后的请求不含 Worker 的原始消息列表。另有测试确认审查者亲自运行 `app.py`：第一次输出 `1` 被打回，修改后输出 `2` 才通过；连续失败时第 3 次审查的 `sendback` 为假。
