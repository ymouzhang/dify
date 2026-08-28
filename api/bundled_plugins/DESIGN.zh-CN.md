# Dify 内置离线插件设计与使用指南

## 1. 文档目的

本文说明 Dify `feature/1.16.1` 分支中的内置离线插件能力，重点包括：

- 为什么要把常用插件随 Dify 一起交付；
- 插件源码、依赖和运行时包如何组织；
- 联网构建与离线部署如何分离；
- 新工作区和已有工作区如何自动安装插件；
- 开发人员如何审查、构建、验证和升级插件。

当前内置两个插件：

| 插件 ID | 版本 | 作用 |
| --- | --- | --- |
| `langgenius/openai_api_compatible` | `0.0.64` | 接入 vLLM、SGLang、LiteLLM 等 OpenAI 兼容模型服务 |
| `langgenius/agent` | `0.0.47` | 提供 Function Calling、ReAct 等 Agent 策略 |

## 2. 背景

标准 Dify 通过 Marketplace 下载插件，并由 plugin daemon 为插件创建独立 Python 运行环境。这种方式适合
联网部署，但在完全离线环境中存在三个交付问题。

### 2.1 安装依赖外部服务

官方 `.difypkg` 可能只包含插件代码。plugin daemon 首次安装时仍需要访问 PyPI，下载插件的直接依赖和
传递依赖。离线环境即使已经拿到插件包，也无法完成运行环境初始化。

### 2.2 人工操作容易产生版本漂移

手工下载插件、解析依赖、收集 wheels、重新打包和上传，会使同一个 Dify 版本在不同时间得到不同内容。
插件版本、依赖版本、Python ABI 或目标 CPU 架构任一不一致，都可能导致离线交付不可复现。

### 2.3 只有二进制包不利于代码审查

`.difypkg` 本质上是 ZIP，但 Git 无法直接展示其中的源码差异。只提交最终包无法清楚回答以下问题：

- 插件代码来自哪个 Marketplace 版本；
- 当前包包含哪些业务源码；
- 依赖版本和 wheel 文件如何确定；
- 最终包是否可以从仓库内容重新生成。

因此，本方案同时保存可审查源码、依赖锁定信息和 plugin daemon 所需的最终包。

## 3. 设计目标

### 3.1 目标

- 离线部署和工作区初始化不访问 Marketplace 或 PyPI；
- 插件源码作为普通 Git 文件，可以直接 review 和 diff；
- 固定插件版本、依赖版本、wheel 文件及 SHA-256；
- 联网机器可以从仓库内容确定性生成 `.difypkg`；
- 离线镜像构建可以验证最终包，无需重新下载依赖；
- 新工作区自动安装内置插件；
- 已有工作区升级后自动补装；
- 重复执行保持幂等；
- 继续使用 Dify 原有 plugin daemon、租户隔离和插件生命周期。

### 3.2 非目标

- 不把插件业务代码合并进 Dify API 进程；
- 不改变 plugin daemon 的安装协议；
- 不在运行容器中动态下载或生成插件包；
- 不自动跟随 Marketplace 最新版本；
- 不为任意第三方插件提供通用离线镜像服务；
- 当前离线依赖不支持 ARM64。

## 4. 总体架构

方案将插件交付拆成两个阶段。

```text
联网构建阶段

plugins/ 中可审查源码
       +
requirements.txt 依赖版本
       +
wheels.sha256 文件锁
       +
packages/manifest.json 来源和构建信息
       │
       ▼
package_plugins.py build
       │
       ├── 临时下载 CPython 3.12 / Linux x86_64 wheels
       ├── 校验每个 wheel 的文件名和 SHA-256
       ├── 注入 no-index 离线配置
       └── 确定性生成 packages/*.difypkg


离线交付阶段

packages/*.difypkg
       │
       ▼
package_plugins.py verify / Dockerfile 构建校验
       │
       ▼
Dify Worker 与 plugin_initializer
       │
       ▼
PluginService → plugin daemon → 工作区插件运行环境
```

联网阶段负责获取依赖并生产制品；离线阶段只使用已经生成且经过摘要校验的制品。

## 5. 目录设计

`api/bundled_plugins` 顶层保持精简：

```text
api/bundled_plugins/
├── plugins/
│   ├── agent/
│   │   ├── manifest.yaml
│   │   ├── main.py
│   │   ├── requirements.txt
│   │   ├── wheels.sha256
│   │   └── ...插件源码、配置和测试
│   └── openai_api_compatible/
│       ├── manifest.yaml
│       ├── main.py
│       ├── requirements.txt
│       ├── wheels.sha256
│       └── ...插件源码、配置和测试
├── packages/
│   ├── manifest.json
│   ├── langgenius-agent-0.0.47-offline.difypkg
│   └── langgenius-openai_api_compatible-0.0.64-offline.difypkg
├── package_plugins.py
├── README.md
└── DESIGN.zh-CN.md
```

各部分职责如下：

| 路径 | 职责 |
| --- | --- |
| `plugins/<插件>/` | 保存可审查的插件实现、声明、资源和测试 |
| `plugins/<插件>/requirements.txt` | 固定完整依赖版本，并声明从包内 `wheels/` 离线安装 |
| `plugins/<插件>/wheels.sha256` | 固定允许打包的 wheel 文件名和内容摘要 |
| `packages/manifest.json` | 同时作为 Dify 运行时清单和构建来源清单 |
| `packages/*.difypkg` | 唯一包含 wheel 二进制的最终运行时制品 |
| `package_plugins.py` | 提供统一的 `build` 和 `verify` 命令 |

仓库不提交独立 `wheelhouse/`。wheel 只在联网构建的临时目录中出现，并最终写入 `.difypkg`，避免保存两份
相同二进制依赖。

## 6. 清单与来源信息

`packages/manifest.json` 中每个插件条目包含两类信息。

运行时字段：

- `plugin_id`：插件 ID；
- `version`：固定版本；
- `file`：最终包文件名；
- `sha256`：最终 `.difypkg` 摘要。

构建与来源字段：

- `source_dir` 和 `source_tree_sha256`：源码位置及源码树摘要；
- `requirements_lock` 和 `wheel_lock`：依赖版本及 wheel 哈希锁；
- `python_version` 和 `pip_platforms`：目标 Python 与平台标签；
- `marketplace_url`：明确版本的官方包地址；
- `marketplace_package_sha256`：官方原始包摘要；
- `marketplace_unique_identifier`：Marketplace 内容标识；
- `retrieved_at`：原始包获取日期。

Dify 运行时只读取安装所需字段，打包工具读取完整条目。这样无需维护额外 build 或 provenance 目录，也能
保留来源审计信息。

## 7. 插件包构建

### 7.1 构建环境

构建必须在可信且可以访问 Python 包索引的 Linux 环境中执行，并具备：

- Python 3；
- pip；
- 对配置的 Python 包索引具有读取权限。

默认使用 `https://pypi.org/simple`。需要使用企业内网镜像时，可以设置：

```bash
export PIP_INDEX_URL=https://pypi.example.com/simple
```

镜像只能改变下载位置，不能改变允许进入包内的文件。下载结果必须与 `wheels.sha256` 完全一致。

### 7.2 执行构建

在 Dify 仓库根目录执行：

```bash
python3 api/bundled_plugins/package_plugins.py build
```

对每个插件，构建器会：

1. 校验 `plugins/<插件>/` 源码树摘要；
2. 根据 `requirements.txt` 下载 CPython 3.12、Linux x86_64 wheels；
3. 对比下载结果与 `wheels.sha256`，拒绝缺失、额外或摘要变化的文件；
4. 删除打包辅助文件 `wheels.sha256`；
5. 在 `pyproject.toml` 中写入 uv 的 `no-index` 和本地 `find-links` 配置；
6. 把临时 wheels 放入包内 `wheels/`；
7. 按固定文件顺序、时间戳、权限和压缩参数生成 ZIP；
8. 覆盖 `packages/` 中对应的 `.difypkg`。

构建器只写入最终包，不会在仓库中留下松散 wheel。

### 7.3 更新制品摘要

构建完成后计算：

```bash
sha256sum api/bundled_plugins/packages/*.difypkg
```

确认内容经过 review 后，将新摘要更新到 `packages/manifest.json`。随后执行验证命令。

## 8. 离线验证

验证命令不访问网络：

```bash
python3 api/bundled_plugins/package_plugins.py verify
```

验证内容包括：

- 仓库中不存在松散 `.whl`；
- 源码树摘要与 manifest 一致；
- `.difypkg` 摘要与 manifest 一致；
- ZIP 可以完整读取且不包含目录条目；
- 包内不保留已失效的 Marketplace 签名和远程 `uv.lock`；
- 包内 wheel 集合与 `wheels.sha256` 完全一致；
- 每个 wheel 的 SHA-256 正确；
- `requirements.txt` 和 `pyproject.toml` 明确禁止访问外部索引；
- 包内 `author/name/version` 与 manifest 声明一致。

`api/Dockerfile` 在 Worker 镜像构建时执行相同验证，因此 Docker 构建不需要访问 Marketplace 或 PyPI。

## 9. Dify 运行时集成

### 9.1 安装服务

`api/services/plugin/bundled_plugins.py` 负责：

- 加载运行时 manifest；
- 读取并重新计算包摘要；
- 调用 `PluginService.upload_pkg` 解析插件包；
- 校验实际 author、name 和 version；
- 查询租户已经安装的 unique identifier；
- 只提交尚未安装的 identifier；
- 等待 plugin daemon 返回安装结果。

所有查询和安装均携带 `tenant_id`，安装状态不会跨工作区共享。

### 9.2 新工作区

新工作区创建后，Dify 原有 `tenant_was_created` 事件会触发默认插件 Celery 任务：

```text
创建工作区
  → install_default_plugins_task
  → 识别 NEW_USER_DEFAULT_PLUGIN_IDS 中的内置插件
  → 从 packages/ 读取并校验本地包
  → plugin daemon 安装
```

内置插件 ID 不访问 Marketplace。配置中的其他插件仍保持 Dify 原有行为。

### 9.3 已有工作区

`api/bundled_plugin_initializer.py` 与 Compose 中的 `plugin_initializer` 一次性服务负责已有工作区：

1. 等待 API、数据库、Redis、Worker 和 plugin daemon 就绪；
2. 查询现有租户；
3. 为每个租户调用同一套内置插件安装服务；
4. 已经安装相同 unique identifier 时直接跳过；
5. 全部租户处理完成后正常退出。

新旧工作区最终使用相同的包读取、身份校验和安装逻辑。

## 10. 构建和部署 Dify

### 10.1 环境配置

`docker/.env` 需要包含：

```dotenv
NEW_USER_DEFAULT_PLUGIN_IDS=langgenius/openai_api_compatible,langgenius/agent
FORCE_VERIFYING_SIGNATURE=false
```

离线包加入 wheels 和离线配置后，内容已经不同于 Marketplace 原始签名，因此受控的离线部署关闭 plugin
daemon 强制签名校验。完整性改由源码 review、来源记录、wheel 哈希锁、最终包 SHA-256 和镜像摘要共同保证。

### 10.2 构建 Worker

```bash
cd docker
docker compose build worker plugin_initializer
```

构建过程中 `api/Dockerfile` 会运行：

```bash
python package_plugins.py verify
```

该步骤仅校验已提交制品，不下载插件依赖。

### 10.3 启动

```bash
docker compose up -d
docker compose ps
```

管理员创建第一个工作区后，Worker 会自动安装两个默认插件。已有工作区由 `plugin_initializer` 补装。

## 11. 开发与质量检查

插件源码保留在 `plugins/` 中，修改插件行为时应直接 review 对应 Python、YAML 和测试差异。由于这些文件来自
独立插件项目，Dify 的 Ruff 配置不自动格式化它们，避免无关格式变化污染来源审计。

修改本功能后至少执行：

```bash
# 联网环境：重建最终包
python3 api/bundled_plugins/package_plugins.py build

# 联网或离线环境：校验源码和最终包
python3 api/bundled_plugins/package_plugins.py verify

# 检查打包工具
uvx ruff check --config api/.ruff.toml api/bundled_plugins/package_plugins.py
uvx ruff format --check --config api/.ruff.toml api/bundled_plugins/package_plugins.py

# Dify 原有内置插件安装测试
uv run --project api pytest \
  api/tests/unit_tests/services/plugin/test_bundled_plugins.py \
  api/tests/unit_tests/tasks/test_install_default_plugins_task.py

# Compose 与实际镜像
docker compose -f docker/docker-compose.yaml config --quiet
docker compose -f docker/docker-compose.yaml build worker
```

## 12. 插件升级流程

插件不会自动升级。升级必须作为一次完整源码变更处理：

1. 选择明确的 Marketplace 插件版本；
2. 下载官方原始包并记录 URL、SHA-256、unique identifier 和获取日期；
3. review 新旧插件源码差异；
4. 更新 `plugins/<插件>/` 中的源码和运行资源；
5. 重新解析完整 Python 依赖，更新离线 `requirements.txt`；
6. 下载目标平台 wheels，更新 `wheels.sha256`；
7. 更新 `packages/manifest.json` 中的版本、来源、源码树摘要和目标平台；
8. 执行 `package_plugins.py build`；
9. 更新 manifest 中最终 `.difypkg` 的 SHA-256；
10. 执行 `package_plugins.py verify` 并重复构建，确认输出稳定；
11. 构建 Worker 镜像并验证新工作区、已有工作区的安装；
12. 提交到 `feature/1.16.1`，经 review 后更新父项目 submodule 指针。

不能只替换 `.difypkg`，也不能只修改源码而不更新依赖锁、来源字段和内容摘要。

## 13. 安全边界

- 只接受明确来源、经过 review 的插件源码；
- Marketplace 原包、源码树、wheel 和最终包分别固定摘要；
- `wheels.sha256` 必须随依赖升级显式变更；
- 最终包中不保留已经失效的上游签名文件；
- 关闭强制签名校验仅适用于受控构建和受控离线交付；
- 不应把 `BUNDLED_PLUGIN_DIR` 指向普通用户可写目录；
- 不应在运行容器内替换 `.difypkg`；
- 发布离线镜像时应记录 Dify commit、父项目 submodule commit、镜像摘要和插件包摘要。

## 14. 当前限制

- 依赖目标为 CPython 3.12、Linux x86_64；
- 最终 `.difypkg` 包含完整依赖，会增加仓库和 Worker 镜像体积；
- 源码目录保留插件自身测试和资源，因此文件数量由插件实现决定；
- `plugin_initializer` 按工作区执行，首次升级耗时随租户数量增长；
- 非内置插件仍需要 Marketplace、内部插件源或单独的离线制品；
- 升级 Dify 时需要重新 review PluginService、默认插件任务和 plugin daemon 接口兼容性。

这些限制应在迁移 CPU 架构、升级 Python、增加内置插件或扩大租户规模时重新评估。
