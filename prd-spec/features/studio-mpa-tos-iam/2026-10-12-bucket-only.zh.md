# Studio MPA TOS IAM 挂载

[English](2026-10-12-bucket-only.md)

- 变更 ID：`studio-mpa-tos-iam`
- 日期：2026-10-12
- 状态：implemented；定向检查 verified；基线全量门禁 blocked；云端 not_run
- 基线：`superops/main`，`035387d1`
- 契约：[Studio MPA 创建](../../../specs/studio-mpa-creation/README.zh.md)，CON-17。

## 背景与范围

Studio 目前要求同时提供 `tosAccessKey`、`tosSecretKey`、`tosBucket`。
`mpa_tool.build_tos_mount_config` 固定输出 AK/SK 鉴权；AgentKit SDK 支持
不带 Credentials 的 `TOS_CREDENTIAL_TYPE_IAM_ROLE`。已有 managed IAM
协调器保留额外绑定、拒绝冲突策略，但平台 TOS 策略没有业务桶写权限。

用户于 2026-10-12 确认 Studio 只选桶，并自动增量补齐桶授权。本次只修改
TOS 挂载鉴权，不改 Studio 登录、Runtime KeyAuth/JWT 或 Worker 协议。
最初实现范围不含发布。用户于 2026-10-12 随后明确授权提交推送、更新现有云端
Studio 并创建测试 MPA；创建桶、修改公开策略或迁移已有会话仍不在范围内。

## 需求与场景

### 已批准的桶前缀调整

2026-10-12 用户将 Studio 可选桶收窄为桶名以 `agentkit-platform-` 开头。
选择器在已有资源接口传入可选 `namePrefix`，按大小写敏感的精确前缀匹配，并在
搜索、计数和分页之前过滤。不传前缀的通用部署查询保持原行为。Studio 创建拒绝
其他桶及其恢复草稿，且在云调用前失败；独立 CLI/Worker 桶名校验保持不变。
本调整不修改角色已有策略、桶公开策略或会话路径。测试允许/排除/伪相似桶名、
空选择、旧草稿、参数传输及分页；人工设计复核确认这些边界。
此前版本已部署为 V41，环境变量未变，前端文件哈希匹配。用户提出本调整前，
尚未提交 MPA 或绑定桶策略；云端创建及全链路验证暂缓。

前缀调整验证：新增 8 个 Python 用例和 3 个 Vitest 用例。相关 Python 754/754，
创建窗口 46/46，完整前端 1,377 个 Node 测试及 73 个 Vitest 用例通过。构建、
前端产物（113 文件/350 引用）、i18n、Ruff、改动文件 Pyright 和 pre-commit 通过。
本轮四个目标文件相对 HEAD 的可执行增量行覆盖率为 100%（Python 22/22、前端
10/10），utree 使用 coverage.py XML 和 Istanbul Clover XML 统计。先确认缺少前缀
逻辑时测试失败，再同步相关 Studio fixture，没有放宽授权顺序或失败传播断言。
全量 Python 再跑：6,495 通过、6 失败、2 收集错误、19 跳过、2 xfailed、6 subtests
通过；失败的 Harness 用例和可选依赖缺失均为此前已复现的基线阻塞。
本地浏览器通过模拟响应确认仅展示匹配桶，临时验证页和 Vite 已清理。未报告存量
缺陷，这是已批准的范围变更。以上为发布前证据快照；后续已授权的发布和云端
Role 挂载验证仍待执行，将与本地验证结果分开记录。

- FR-1：只展示一个可选桶选择器，复用资源列表 hook 与 DeploymentSelect；
  保留加载、错误、空列表、搜索、分页、重试、禁用、键盘和取消行为。不展示 TOS AK/SK。
- FR-2：Studio 提供桶时使用实际 Worker Tool 角色的 IAM Role 鉴权；安全拒绝
  非法桶名和旧客户端非空凭据提交，清除浏览器恢复草稿中的旧凭据。未选桶保留
  服务端 profile（内置 profile 默认不挂载）。CLI/YAML AK/SK 模式保持兼容；显式 `tos-credential-type: iam_role`
  支持仅配置桶的 managed profile。
- FR-3：在工作负载身份、数据库、网络写操作前，核验桶属于部署账号且地域匹配，再协调稳定的
  桶专属自定义策略与 Worker 角色的 Global 绑定。验证账号、信任、策略内容和回读；
  保留额外策略及信任。冲突即失败；策略缺失、并发创建/绑定用有界超时恢复。
- FR-4：只在所选桶授予 `tos:HeadBucket`、`tos:ListBucket`；只对其
  `sandbox-session/*` 对象授予 `tos:GetObject`、`tos:PutObject`、
  `tos:AbortMultipartUpload`、`tos:DeleteObject`。不涉及其他桶、CreateBucket、
  桶策略/ACL、KMS 授权或全对象通配范围。
- FR-5：Tool 挂载使用 IAM Role 且不带 Credentials；保持 `/data/output`、
  规范的逐会话路径和 Runtime 非敏感挂载开关/桶名。复用核验同时检查鉴权模式
  与角色，不能只检查路径。不得隐式修改已有 Tool 和 Session。
- FR-6：权限、归属、配置和策略冲突在 IAM 阶段安全失败，返回本地化错误。
  取消保留 IAM 资源供重试；云调用使用新读取并核验账号的部署凭据。

## 设计与兼容

为 managed Worker 增加可选鉴权模式，CLI 历史默认仍为 AK/SK。Studio 请求仅
包含 `tosBucket`，固定 runner 应用 IAM Role。任务身份、锁定、取消契约保持不变。
复用已有 IAM SDK 边界、规范策略比较与错误白名单，增加独立命名的桶策略，
不改 `VeADKMPARuntimeAccessV1`。桶专属稳定策略名避免不同桶之间读改写竞争，
同一请求的并发操作可收敛。

ListBucket 暴露该桶元数据，但对象访问只允许会话输出前缀。共享角色的所有
使用者都会获得这个前缀权限，逐会话目录不是租户授权。SSE-KMS 桶需要另行
批准密钥权限。旧 Studio 客户端提交凭据字段会得到安全校验错误，需要刷新。
旧 CLI AK/SK 请求和普通智能体部署不变。

## 任务与验收

| 需求 | 任务 | 验收 | 证据 |
| --- | --- | --- | --- |
| FR-1、FR-2 | T-1：UI/API/config | AC-1：仅桶请求、清除旧密钥、选择器状态 | config/routes 和 43 个窗口用例通过；隔离浏览器通过 |
| FR-3、FR-4、FR-6 | T-2：IAM ensure 与编排接线 | AC-2：精确权限范围、重复/并发重试、失败/取消边界 | IAM/service 测试通过 |
| FR-5 | T-3：Tool 参数与复用核验 | AC-3：无 Credentials，严格核验模式/角色，旧模式不变 | Tool/worker 测试通过 |
| 全部 | T-4：文档/产物/门禁 | AC-4：定向/全量测试、构建/产物、浏览器状态、可执行增量覆盖率 >95% | 定向/构建/产物/浏览器/覆盖率通过；全量回归和 Pyright 存在已复现的基线阻塞 |

先执行相关 pytest，再执行
`uv run --extra dev pytest -n 2 -m 'not codex_smoke and not piagent_smoke'`、
`npm --prefix frontend test`、构建、产物核验、改动 Python 文件 Ruff/Pyright、
i18n、密钥扫描与 diff 空白检查。覆盖率统计可执行改动行，不以整文件替代。
浏览器检查使用隔离的模拟云响应；未单独授权的真实 IAM/挂载 E2E 标记 not_run。

## 评审与交付

review-spec 技能不可用，以人工设计评审替代，检查角色归属、增量策略范围、旧 CLI
兼容、重试/取消、本地化和双语一致性。ui-ux-pro-max 不可用，采用仓库 SPEC、
Foundation 和已有选择器行为。用户明确授权业务功能实现，单测技能的业务编辑
限制不覆盖此授权；测试仍严格断言正确行为。

## 验证与变更记录 — 2026-10-12

修改 Studio 请求/窗口、managed Worker 配置、IAM 协调、服务编排和 Tool 元数据
核验，同步双语组件契约与操作指南。无需修改 MPA Agent/Worker 源码，已有会话
挂载路径不变。复用资源选择器及其已有样式，没有新建 UI 系统。已删除临时浏览器
验证页源码并停止隔离的 Vite 服务，重新生成正式前端产物。

- **pass：**Python 3.12.12，`tests/integrations/mpa_managed` 和
  `tests/integrations/test_mpa_tool.py` 共 719/719；相比基线 677 个净新增 42 个。
  契约测试在实现前确实失败。覆盖率插件环境下，Pydantic 延迟导入导致收集失败；
  在 `pytest.main([...])` 前显式导入 `pydantic.RootModel` 后，同样 719 个测试可以
  正常统计覆盖率，未修改业务代码或依赖。基线 service 收集也复现了该覆盖率
  导入错误。最终验证未采用 Python 3.13。
- **pass：**可执行增量行覆盖率 100%：Python 97/97，由 `utree coverage parse`
  对 diff hunks 和 coverage XML 统计；前端 9/9，由 Istanbul statement 起始行与
  改动行求交。排除未改动代码、纯类型、测试、文档及生成包；不是整文件或分支覆盖率。
- **pass：**`npm --prefix frontend test`：1,377 个 Node 测试及 70 个 Vitest 用例；
  `npm --prefix frontend run build`、`check:i18n`、`test:webui-assets`（113 文件、
  350 内部引用）、Ruff、pre-commit 全文件和 `git diff --check`。Vite 保留已有大包警告。
- **pass：**真实本地浏览器、模拟 API 响应：正常选择、无 TOS 密钥输入、加载、
  空列表、错误/重试、搜索、分页、键盘选择、中文搜索输入、关闭、390px 窄窗口。
  这不是云端 IAM/挂载验证，也不是操作系统真实 IME composition 测试。
- **fail，已复现基线：**全量并行回归：6,485 通过、6 失败、2 收集错误、19 跳过、
  2 xfailed、6 subtests 通过。使用相同解释器，在隔离的 `035387d1` 归档中复测
  对应六个 Harness 用例和两个 self-host Sandbox 收集错误，完全一致（60 通过）。
  原因是缺少可选 `llama_index`/`anthropic` 依赖，未修改无关业务或安装依赖来掩盖门禁。
- **fail，已复现基线：**Pyright 使用 `.venv/bin/python`，在 `mpa_tool.py` 未改动的
  历史 SDK 调用中报 15 个签名错误；临时复制的原始基线文件也报相同 15 个错误，
  已删除临时文件。其他改动的生产 Python 文件无 Pyright 诊断。
- **not_run：**真实 IAM 写入、云端 Role 挂载会话 E2E、部署、镜像发布、提交或推送。
  既有 Tool/Session 未变更。共享角色权限、已有 Deny、桶公开策略和 KMS 限制仍需
  操作方知悉；策略成功绑定不代表已证明云端文件读写成功。

实现评审检查稳定桶策略名、账号/地域/信任核验、精确对象前缀、增量幂等/并发、
安全失败、超时/取消及旧 CLI 兼容。未引入已有策略覆盖或全对象权限。全量/类型
门禁不能表述为通过，云端验收与本地实现仍然独立。
