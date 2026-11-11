# 香港旅行规划：Baseline 与 ImprovementV2

项目使用 LangChain 调用大模型，根据用户需求生成香港旅行行程。两个版本共用景点、住宿、测试案例和离线评分器：

| 版本 | 模型生成时可读取的数据 | 入口 |
| --- | --- | --- |
| Baseline | 景点、住宿 | `baseline/baseline.py` |
| ImprovementV2 | 景点、住宿、离线路线 | `ImprovementV2/addRouteSet.py` |

两个版本目前都是单次大模型生成：模型不会调用路线校验工具或根据评分结果自动修改行程。共享评分器在生成后使用离线路线数据评分，不把评分结果反馈给模型。两版提示词的主体一致，路线数据的使用说明不同。历史运行可能使用旧提示词，比较时请确认版本。

## 本地准备

需要 Python 3.10 或以上，推荐 3.12。在项目根目录执行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

如果已经有可用的 `.venv`，激活并安装依赖即可，无需重新创建。没有 `python3.12` 时，请安装 Python 3.12，或使用本机已安装的其他 Python 3.10+ 解释器。

创建本地配置：

```bash
[ -f .env ] || cp .env.example .env
```

编辑 `.env`，填写 OpenRouter API Key 和你有权限使用的模型 ID：

```bash
export OPENAI_API_KEY='你的 OpenRouter API Key'
export OPENAI_BASE_URL='https://openrouter.ai/api/v1'
export TRAVEL_MODEL='你的 OpenRouter 模型 ID'
export ORS_API_KEY=''
```

`ORS_API_KEY` 仅供需要 openrouteservice 的额外数据处理使用；运行本项目已有数据集和评分器不需要它。不要把包含真实密钥的 `.env` 提交或打包。

每次新开终端，进入项目目录后执行：

```bash
source .venv/bin/activate
source .env
```

## 运行实验

真实运行需要网络和可用的模型 API；费用取决于服务商和模型。下面的两条命令分别运行开发集同一个案例，并自动评分：

```bash
python baseline/baseline.py run --split dev --case DEV-01
python ImprovementV2/addRouteSet.py run --split dev --case DEV-01
```

完整运行开发集（30 条）：

```bash
python baseline/baseline.py run --split dev
python ImprovementV2/addRouteSet.py run --split dev
```

运行测试集（15 条）：

```bash
python baseline/baseline.py run --split test
python ImprovementV2/addRouteSet.py run --split test
```

建议先在开发集调试并确定配置，再用测试集做最终比较。运行时两组应使用相同模型和配置；模型输出具有不确定性，不保证与已有结果完全一致。ImprovementV2 会向模型提供路线数据，输入 token 通常更多。

如果只想查看模型将收到的内容，不调用 API，可添加 `--preview`：

```bash
python baseline/baseline.py run --split dev --case DEV-01 --preview
python ImprovementV2/addRouteSet.py run --split dev --case DEV-01 --preview
```

也可使用 `python baseline/baseline.py chat` 或 `python ImprovementV2/addRouteSet.py chat` 交互输入需求；输入 `/reset` 清空对话，`/exit` 退出。

## 查看结果

每次批量运行会创建独立的时间戳目录：

```text
results/baseline/运行目录/          # ImprovementV2 在 results/ImprovementV2/ 下
├── scores.json                    # 本次汇总及各案例指标
└── DEV-01/
    ├── result.jsonl               # 原始模型输出与运行信息
    ├── result.md                  # 可读行程
    ├── result.score.md            # 评分明细与未判定原因
    └── result.score.json          # 本案例的评分数值和原因
```

每项指标使用 `数值` 和 `原因` 子字段；`null` 表示该项无法判断或不适用。行程可行率和顺路达标率的分母为已生成的行程天数，无法判断的日期会记录原因。结果中 `status: ok` 只表示模型请求成功，不代表输出格式或行程正确。

重新对已有运行目录离线评分，无需调用模型；输出文件必须不存在：

```bash
python evaluation/score_baseline.py results/baseline/运行目录 --json-output 重评分.json
```

评分规则及局限见 [evaluation-rubric.md](docs/evaluation-rubric.md)。路线数据是固定参考时间下的估算，不等同于实际出行当天的实时交通；公众假期及临时开放调整不在当前数据范围内。

## 项目结构与测试

- `dataset/`：景点、住宿、路线数据及开发集、测试集。
- `evaluation/`：两个版本共用的评分器。
- `scripts/`：数据处理与结果展示脚本。
- `docs/`：项目说明、评分规则、路线数据说明及课程材料。
- `results/`：实验结果。

运行本地自动测试，不调用模型：

```bash
python -m unittest discover -s tests -q
```
