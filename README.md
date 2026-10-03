# 离线原料配比试算台（虚构工艺边界 · 工艺研发用）

> ⚠️ **边界声明**：本应用使用的原料名称、化验单数值、成本、可用量与率值窗口均为**虚构演示数据**，
> 仅用于工艺研发离线比较“原料成本 ↔ 生料化学指标”的取舍。
> 应用不连接任何生产控制系统，**不向真实生产设备下发指令**。

## 技术栈

| 层 | 技术 | 职责 |
|---|---|---|
| 前端 | Angular 18（standalone 组件，纯 CSS 堆叠条） | 氧化物来源/配比比例展示、试算交互、方案对比、化验追溯 |
| 后端 | FastAPI + Pydantic | REST API、干湿基换算、错误码、静态托管 |
| 优化 | SciPy `linprog`（HiGHS） | 线性规划：成本最优 / 廉价料最大 / 率值居中 |
| 存储 | PostgreSQL 15 | 原料、**多版化验单**、试算批次、方案、逐原料换算留痕 |

## 计算口径

1. **先质量守恒合成，再算率值**（不把率值当输入去反推成分）。
   干基份额 x_i（Σx_i=1）：`合成干基% = Σ x_i × 原料干基%`。
2. 率值（分母为零即报错，见下）：
   - 硅率 `SM = SiO2 / (Al2O3 + Fe2O3)`
   - 铝率 `IM = Al2O3 / Fe2O3`
   - 石灰饱和系数 `KH = (CaO − 1.65·Al2O3 − 0.35·Fe2O3) / (2.8·SiO2)`
3. **干湿基**：化验按 `dry`（干基）或 `wet`（收到基）登记；
   含水率 w 时 `干基% = 湿基% /(1−w)`，自由水不并入 LOI；
   质量换算 `湿料t = 干料t /(1−w)`，成本按湿料吨价结算。
4. 碱当量 `Na2O + 0.658·K2O`。
5. **检测不确定度（稳健配比）**：化验版可对每个已测组分声明**同一基准**的
   上下偏置（百分点，`lower ≤ 0 ≤ upper`）；湿基容差先按 `×1/(1−w)` 换干基。
   稳健模式对每条 SM/IM/KH/有害组分约束取“**对该约束最不利**”的组分边界
   （不同约束的最坏组合不同，不允许只抽一个幸运样本）。全零容差时稳健解
   与名义解完全一致。

### 硬性错误规则（不以零含量兜底）

- **缺测**：候选原料的必测组分（CaO/SiO2/Al2O3/Fe2O3，及被设上限的有害组分）
  不在 `measured_oxides` 中 → HTTP 422 `MISSING_ASSAY`，返回原料/化验版/单号/缺测项；
- **分母为零**：Fe2O₃、SiO₂ 等为 0 导致 IM/SM/KH 无定义 → HTTP 422 `ZERO_DENOMINATOR`；
- **检测分母区间跨零**：稳健模式下某率值分母（如 IM 的 Fe2O₃）的检测区间可能取到 0
  → HTTP 422 `UNCERTAIN_ZERO_DENOMINATOR`，明确拒绝，不输出无定义率值；
- **容差非法**：区间倒置/方向错 `UNCERTAINTY_BAD_INTERVAL`、下界使含量为负
  `UNCERTAINTY_NEGATIVE_VALUE`、对缺测项声明容差 `UNCERTAINTY_ON_UNMEASURED`、
  格式错 `UNCERTAINTY_BAD_FORMAT`；
- “已实测为 0”（演示料 QZ00）与“未测/缺测”（演示料 SP01）严格区分。

### 约束

- 原料**最低掺量**（干基 %，档案字段）、**湿基可用量**（换算成干基份额上限）；
- **有害组分干基上限**（Cl、碱当量等，可扩展）；
- 率值区间经线性化进入 LP（如 SM≤hi ⇔ `Σ(SiO2−hi(Al2O3+Fe2O3))x ≤ 0`）。

### 求解模式与无解诊断

- `min_cost`：最小元/吨干生料；
- `max_cheap`：两阶段 LP——先最大化指定廉价料份额，再锁定份额最小化成本打破平局；
- `balanced`：率值对区间中点的绝对偏差最小（线性化），轻微成本偏好做次序裁决；
- **稳健模式**（`robust_mode="robust"`）：在同一批 LP 上按约束独立取最坏检测边界，
  返回名义方案 + 并列的稳健方案（最坏率值区间、相对窗口的稳健余量、最坏边界组合）；
  名义合格但边界超限时稳健判不可行，诊断列出**触发的化验报告/组分/边界与突破量**；
- **求解失败**：对全部不等式做“最小违约松弛”模型，列出仍被突破的冲突约束、
  限值、最小违约解达到值与缺口；最低掺量之和 >100% 另有算术预检 `MIN_SHARE_OVERFLOW`。

## 目录

```
backend/
  app/
    main.py        FastAPI 路由 + 错误处理 + SPA 托管
    chemistry.py   干湿基换算 / 质量守恒 / SM/IM/KH / 缺测与零分母异常
    optimizer.py   SciPy HiGHS LP、多模式、冲突诊断
    models.py      SQLAlchemy：material / assay_version / blend_run / solution / item
    crud.py        持久化与历史回看
    schemas.py     Pydantic 模型
    seed.py        虚构演示数据（含湿基化验单、缺测/零分母演示料）
  tests/           21 个 pytest（换算/守恒/报错/求解/API/追溯）
  scripts/         pg_start / pg_stop / seed / serve
frontend/
  src/app/
    components/    materials / blend / solution-card / history / stack-bar
    services/api.service.ts
    models/models.ts
```

## 启动（本机用户态，无需 root/docker）

PostgreSQL 15 以 deb 解包方式安装在 `~/.local/pgsql`，数据目录 `~/.local/pgdata`，
端口 **55432**，库名 **rawmix**，连接串：
`postgresql+psycopg2://mixapp@127.0.0.1:55432/rawmix`（可用 `RAWMIX_DATABASE_URL` 覆盖）。

```bash
# 1) 启动数据库（首次自动 initdb + 建库）
backend/scripts/pg_start.sh
# 2) 写入虚构演示数据
backend/scripts/seed.sh
# 3) 启动 API + 已构建前端（http://127.0.0.1:8000）
backend/scripts/serve.sh
```

前端开发模式（热更新，代理 /api → :8000）：

```bash
cd frontend && ./dev.sh        # http://127.0.0.1:4200
# 生产构建（输出到 backend/static，由 FastAPI 托管）：
cd frontend && ./node_modules/.bin/ng build frontend
```

Python 依赖：`pip install -r backend/requirements.txt`（本机装于用户 site-packages）。

## 前端四个标签页

1. **原料与化验**：全部原料/多版化验单、干湿基标记、缺测红格、干基换算预览；
2. **配比试算与方案对比**：候选/化验版/率值窗口/有害上限/模式选择 +
   **检测不确定度稳健开关**，五个快速场景：
   - 基准三方案对比（含水率差异：粉煤灰 18% 湿基化验单 → 采购湿料量与留痕）；
   - 廉价原料（页岩）致 IM/KH 超限 → 失败 + 冲突项；
   - 碱当量上限收紧（0.40%）→ 有害组分冲突与突破量；
   - 5000 t 大批量 → 湿基可用量与 KH 同时冲突；
   - **稳健：名义合格 · KH 检测边界失效** → 稳健不可行，列报告/组分/突破量；

   方案卡并列展示**名义方案**与**稳健子方案**：最坏率值区间、相对窗口的稳健余量
   （绿=安全/红=被顶破）、最坏边界组合、稳健干基配料比例与逐组分干基上下界留痕；
3. **手工配比**：一键装入“100% 零铁石英（IM 分母为零）”和“缺测矿样（MISSING_ASSAY）”；
4. **历史追溯**：每个方案可追到批次号、原始化验版本/单号、原始 wet/dry 报送值、
   逐组分湿→干公式、干/湿料质量、水量与成本算式。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/materials?active_only=` | 原料与全部化验版本（含 uncertainty） |
| POST | `/api/blend` | 试算（多模式、约束、可入库、`robust_mode` 名义/稳健、请求级容差覆盖） |
| POST | `/api/evaluate` | 手工份额合成 + 率值（错误演示） |
| GET | `/api/runs` `/api/runs/{id}` | 历史批次与完整追溯（名义值/最坏边界/触发报告/组分留痕） |
| GET | `/api/health` | 健康检查（含 fictional-boundary 标记） |

`robust_mode`：`nominal`（默认，化验值视为精确常量，行为与旧版一致）/
`robust`（响应在每个名义方案下并列返回 `robust` 子方案；
顶层 `robust_status` 为 `robust_feasible`/`robust_infeasible`）。
`candidates[].uncertainty_override` 可对当次试算覆盖容差（仅本次有效、不写库）。
新列经启动时幂等 `ALTER ADD COLUMN` 补入，旧运行记录与原始化验快照保持不变。

错误响应体：`{ "error_code": "MISSING_ASSAY|ZERO_DENOMINATOR|UNCERTAIN_ZERO_DENOMINATOR|UNCERTAINTY_BAD_INTERVAL|UNCERTAINTY_ON_UNMEASURED|UNCERTAINTY_NEGATIVE_VALUE|...", "message": ..., "details": ... }`。

## 测试

```bash
cd backend && python3 -m pytest tests/ -q
# 35 passed（PostgreSQL 不可达时，原 7 个 PG 端到端用例自动跳过，
# 同口径用例由 SQLite 驱动的 test_robust_api.py 覆盖）
```
