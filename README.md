# 禾堤食品 · 包装外观质检台

禾堤食品是一个模拟的 42 人休闲食品厂，三台视觉机覆盖袋装产品的线边外观质检。操作员按批次归档相机读数和缺陷位置，质检主管签核等级，线边库按返工单发放对应膜材。排产人员读取批次工序、余量和关闭结果。

现行交接依靠机台读数、纸面质检单和仓库台账。相机偶发漏出通道，纸单上难以追溯哪次补采覆盖了哪个样本，改判后的领料数量也容易与复测范围脱节。工作台从相机读数和实物观察接手，到放行关闭并交接排产为止。

## 快速启动

```sh
cp .env.example .env
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```

- 工作台 `http://127.0.0.1:18264`，API 文档 `http://127.0.0.1:19264/docs`，PostgreSQL `127.0.0.1:17764`。
- `SEED_DEMO=true` 在启动时填入固定演示账号、批次和材料。自然键已经存在时保留数据库记录。可以重复运行 seed。
- `docker compose down -v --remove-orphans` 停止并删除此项目的数据库、Redis 和凭证卷。需要保留数据时运行 `docker compose down`。
- 本地开发可沿用 Compose 的数据库和队列。在项目根目录运行 `docker compose up -d --build` 重建当前源码。前端使用原生 ES 模块，无打包步骤。后端镜像从 `backend/requirements.txt` 安装依赖并执行 SQL 迁移。

## 岗位与演示账号

三个演示账号口令均为 `PackDemo2026!`。JWT 有效期 8 小时。演示配置面向本机回环地址。

| 账号 | 岗位 | 使用页面和权限 |
|---|---|---|
| `operator` | 线边操作员 | 登记批次、归档同机台样本、标记缺陷、提交判级、按返工单领料 |
| `supervisor` | 质检主管 | 查询批次、确认或驳回缺陷、签核等级、登记返工复测、关闭批次 |
| `viewer` | 排产查看 | 查询批次、样本与模型凭证、返工和交接履历 |

## 一班交接剧本

| 时间 | 动作 |
|---|---|
| 08:10 | 张贴 `PK-261121-02` 青柠米饼 90 g 批次，绑定 `VS-03`，待采样 1,600 件。 |
| 08:25 | 查阅 `PK-261121-01-S01` 海盐燕麦脆片。五通道完整，模型和主管均为 A 级，批次已经关闭。 |
| 08:42 | `VS-01` 的 `PK-261121-03-S01` 伯爵茶曲奇读出褶皱 1.61% 和色差 2.76 ΔE，偏位通道缺失。登记后批次禁止判级。 |
| 08:55 | 操作员打开「归档补采版本」，以 `PK-261121-03-S02` 录入同机台复采读数，偏位填写 0.77 mm。旧版本、旧读数及历史标记继续可查，完整新版本可提交判级。 |
| 09:10 | 主管查看 `PK-261121-05-S01` 山椒豌豆的 B 级建议，以及上封口 x=67%、y=18% 的待复核压痕。确认或驳回后才能签核。 |
| 09:20 | `PK-261121-04-S01` 黑芝麻薄脆模型为 A。主管根据实物封口折边改判 B，记录理由，自动签出 160 件 `FILM-GL-180` 返工需求。 |
| 09:28 | 操作员在 A-12 对 `PK-261121-04` 确认领料。初始 360 件扣至 200 件，重复确认会被拦截。 |
| 10:05 | 主管在 `PK-261121-04-R01` 登记同机台五通道复测，替换件数填写 160。举例可填褶皱 0.32%、偏位 0.19 mm、色差 0.83 ΔE、间隙 0.14 mm、光泽波动 1.9%。复测签核后关闭并交接排产。 |

五个初始批次对应这份剧本。启动时保留一张待采批次、一张缺通道批次、一张待签核批次、一张改判待领料批次和一张已关闭批次。表中时间为模拟班次的操作顺序，界面显示实际归档与签核时间（Asia/Shanghai）。

## 状态与数据规则

- 批次按 `new → blocked/measured → queued → review → rework/approved → closed` 推进。缺通道只能补采，判级失败回到 `measured`，可重新提交。批次由主管关闭。
- 测量版本不可覆盖，保存新版本时链接旧样本。所有通道有单位、边界和输入校验。缺陷标记绑定特定样本和包装面坐标，主管需要为确认或驳回附说明。当前样本的未复核缺陷会拦截等级签核。
- A 同判可以直接进入待关闭。任何改判和任何非 A 级签核都要求记录原因和有效返工件数，在签核事务里生成一张领料单。保留模型原判、人工终判和签核人。
- 返工单按 `requested → issued → verified` 推进。领料扣减对应 SKU，库存不足保持待领料。换膜件数必须等于领料件数，且用绑定机台登记五通道复测。外观复测上限分别为褶皱 1%、偏位 0.5 mm、色差 2 ΔE、封口间隙 0.35 mm、光泽波动 4%。失败记录进入履历，批次继续等待复测。

## 模型与执行

`backend/src/model.py` 使用固定种子 `1065` 从三种等级的合成参考分布生成 216 条训练记录和 72 条独立留出记录。训练集拟合 `StandardScaler`，将五通道按训练均值与标准差变换为 `z = (x - μ) / σ`。PCA 用完整 SVD 保留 3 维，`LinearSVC(C=0.75, dual=False)` 输出 A/B/C 三个一对其余类别的线性间隔分数。模型保存各类分数、主成分坐标、方差贡献、留出指标、参数、输入摘要和参考样本校验和。训练和留出数据为演示用合成数据，复测阈值为本演示工位外观规则。

判级请求先原子写入 PostgreSQL 持久队列，再向 Redis 发通知。Worker 消费通知并轮询队列，通过数据库批次锁保证单次落库。结果和失败原因回到批次详情，成功凭证同时写入共享卷 `/model-evidence`。容器重启发生在提交前时，数据库事务回滚，待判级记录仍可消费。

## 目录与配置

| 位置 | 职责 |
|---|---|
| `backend/migrations/001_workcell.sql` | 批次、测量版本、缺陷、判级、领料单、库存和履历的 SQL 结构与约束 |
| `backend/migrations/002_training_handover.sql` | 膜卷登记链、样本撤销、训练包/留出包、包内样本与互斥唯一索引 |
| `backend/src/inspection.py` | 成批登记、采样版本、缺陷定位和复核前置条件 |
| `backend/src/disposition.py` | 人工签核、领料并发保护、复测和关闭 |
| `backend/src/training_domain.py`、`training.py` | 训练发包纯规则引擎与 PostgreSQL 事务落地、下载令牌 |
| `backend/src/model.py`、`queue.py`、`worker.py` | 可复现线性模型和异步任务 |
| `backend/src/api.py`、`auth.py` | FastAPI 路由、JWT、权限、访问日志与请求编号 |
| `backend/tests/` | 发包规则内存单测与离线路由/模型冒烟（仅标准库可运行） |
| `frontend/src/pages/` | 批次台账、判级工作台、返工领料、数据发包，登录及路由在 `app.js` |
| `frontend/src/api.js`、`ui.js`、`labels.js` | API 客户端、共享业务组件、单位、岗位与状态映射 |

`.env` 可设置 `COMPOSE_PROJECT_NAME`、`FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`POSTGRES_PASSWORD`、`JWT_SECRET` 和 `SEED_DEMO`。Compose 使用 Nginx 1.27、Python 3.12 + FastAPI、scikit-learn 1.6.1、PostgreSQL 16 和 Redis 7。五个服务各有健康检查，Nginx 将 `/api/` 转发给 API。

岗位、等级、机台、缺陷类型、包装面和缺陷复核态出现在 `migrations/001_workcell.sql`、`backend/src/schemas.py` 和 `frontend/src/labels.js`。批次、判级、返工状态出现在 SQL 约束、`inspection.py` / `disposition.py` / `queue.py` 和 `labels.js`。测量通道与单位由 `schemas.py`、SQL 测量列、`model.py`、`labels.js` 和 `ui.js` 共用。返工触发类型在 SQL、`disposition.py` 和 `pages/rework.js`，测量用途在 SQL、`inspection.py`、`disposition.py` 和 `pages/workbench.js`。复测上限从后端 API 返回。

本项目以 MIT License 提供，见 `LICENSE`。


## 训练数据发包（质检 → 训练组交接）

签核读数交给训练组时，以**人工终判**（`grade_decision.final_grade`）作为标签。功能在「数据发包」页（`/datasets`）与 `/training/*` 接口，结构见 `migrations/002_training_handover.sql`、规则见 `backend/src/training_domain.py`（纯函数）与 `backend/src/training.py`（PostgreSQL 事务）。

- **按批登记膜卷，缺卷号不入包**：操作员按样本登记用过的膜卷号（`film_roll_usage`，每样本仅一条当前登记）。发包候选必须同时满足「未撤销 + 已人工终判 + 已登记当前膜卷」，缺卷号样本被跳过并在响应 `skipped.missing_roll` 中列出，不会进入包。
- **训练/留出膜卷互斥，同批同边**：一个膜卷只能归训练（train）或留出（holdout）一侧；同一批次用过的所有膜卷固定到同一边。跨边发包在膜卷级或批次级被拒绝（409）。归边在发包事务内写回 `film_roll_usage.side`。
- **卷号更正先受理先计入**：更正走独立流程（主管），保留历史登记链（`is_current` + `corrected_from_id`）。更正与发包并发时，先提交的更正先生效；更正事务会把包含该样本的**活跃包整包停用**（`deactivation_reason=roll_corrected`），随后的发包读到的是新卷号。新卷号若已归对侧则拒绝更正。
- **样本撤销整包停用**：主管撤销样本（`visual_measure.revoked_at`）后，含该样本的活跃包全部停用（`sample_withdrawn`），撤销样本不再进入后续候选。
- **旧链接立即失效**：下载链接是 HMAC 签名令牌（包 id + 24 小时过期时间，密钥复用 `JWT_SECRET`）。包一旦停用，即使令牌未过期、签名有效，下载端点也返回 410「训练包已停用，旧链接不能下载」。
- **中断恢复重试不新增包**：每次发包对样本集合算 `cohort_hash`（与顺序、重复无关），同 `cohort_hash + purpose` 的活跃包唯一（部分唯一索引 `one_active_cohort`）。重试原样本集合直接复用原包（响应 `retried=true`，HTTP 200），并发撞索引时用 savepoint 回滚后复用；包停用后同 cohort 可重新组包。
- **工作台可查**：数据发包页可查每个样本的膜卷、入包资格、撤销状态；训练包卡片与详情弹窗列出包内样本、膜卷清单和停用原因；判级工作台显示本批膜卷登记与更正轨迹；「业务核验」新增「训练发包与膜卷互斥」模块。

接口（均需登录）：`GET/POST /training/rolls`（操作员登记）、`POST /training/rolls/correct` 与 `POST /training/samples/withdraw`（主管）、`GET/POST /training/packages`（主管发包/重试）、`GET /training/packages/{id}`（包内样本/膜卷/停用原因）、`POST /training/packages/{id}/link`（签发链接）、`GET /training/packages/{id}/download?token=`（下载 JSON 快照，停用即 410）。

业务规则用标准库即可验证，无需数据库或第三方依赖：

```sh
cd backend && python3 -m unittest discover -s tests -v
```

`tests/test_training_rules.py` 以内存假库驱动纯函数引擎，覆盖登记/缺卷、膜卷与批次互斥、更正停用与链接失效、撤销停用、重试幂等、令牌签名/过期/篡改等 30 个场景；`tests/test_offline_smoke.py` 用 stub 验证 Pydantic 模型与 FastAPI 路由装配。


## 业务核验模块

登录后打开页面右下角的「业务核验」。可从实时业务台账筛选记录、导出所选结果，主管可对当前证据签认并填写说明。

- **返工物料追溯** 沿返工单核对领用 SKU、库位和复测证据，用于班末交接和缺证补录。
