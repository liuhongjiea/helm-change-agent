# Helm Change Agent

把 Helm 变更评审从「跟 AI 对话」变成「点四下 + 看进度条 + 读报告」的本地 Web 工具。

## 背景

评审一次组件变更原本需要：描述组件名 → 等 AI 跑 helm template → 等 kubectl 拉集群状态 → 等 AI 分析 → 读多轮对话。流程对，但交互繁琐、耗时且难以分享。

这个工具把流程固化成 Web UI，所有确定性步骤（render / fetch / diff）在后端自动串行执行，只保留 AI 风险分析这一步调用 LLM，其余不依赖对话。

## 效果

```
选集群  →  选组件  →  选连接方式  →  开始分析
                                         │
                        ┌────────────────▼────────────────┐
                        │  ● helm template  ✓  1.2s       │
                        │  ● kubectl fetch  ✓  3.4s       │
                        │  ● yaml diff      ✓  0.1s       │
                        │  ● AI 风险分析    ✓  8.5s       │
                        └─────────────────────────────────┘
                                         │
                              自动跳转报告页
```

报告包含：AI 智能分析 · 配置变更详情（字段级 diff）· 资源清单 · 操作建议 · 回滚预案，格式与原 skill 生成的 markdown 完全一致，历史报告共存于同一目录。

## 前置依赖

| 依赖 | 用途 | 获取方式 |
|------|------|----------|
| Python 3.11+ | 运行服务 | `brew install python` |
| `helm` | `helm template` 渲染 | `brew install helm` |
| `kubectl` | 拉取集群资源 | `brew install kubectl` |
| `miks-iam-tool` | miks-proxy 模式生成 kubeconfig | 内网下载 |
| `miks-config` 仓库 | 集群/组件索引 | 内部 git |

## 安装

```bash
# 1. 克隆仓库
git clone https://github.com/liuhongjiea/helm-change-agent.git
cd helm-change-agent/web

# 2. 安装 Python 依赖
pip3 install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 API Key 和路径（见下方说明）
```

## 配置

`.env` 各字段说明：

```bash
# Anthropic 兼容网关（公司内网）
ANTHROPIC_BASE_URL=http://model.mify.ai.srv/anthropic
ANTHROPIC_API_KEY=sk-xxxxxxxxx
ANTHROPIC_MODEL=ppio/pa/claude-opus-4-7   # 注意：需带 ppio/pa/ 前缀
ANTHROPIC_MAX_TOKENS=8192

# miks-config 仓库本地路径（用于读取集群/组件信息）
MIKS_CONFIG_DIR=/path/to/miks-config

# 报告输出目录（markdown 文件写到这里）
REPORTS_DIR=/path/to/helm-change-agent

# miks-proxy 凭证文件（来自 helm-change-review skill 安装时的 creds.env）
# 格式：export MIKS_IAM_AK=...  export MIKS_IAM_SK=...  export MIKS_IAM_SA=...
SKILL_CREDS_FILE=/path/to/creds.env
```

## 启动

```bash
python3 run.py
```

浏览器访问 **http://127.0.0.1:8080**，服务仅绑定本地回环地址，不对外暴露。

支持热重载：修改 `server/` 下的 Python 文件后自动重启，无需手动操作。

## 使用流程

1. **选集群** — 从下拉框搜索/选择目标集群（索引来自 `miks-config/cluster-config/*.yaml`）
2. **选组件** — 列表自动过滤该集群安装的组件（来自 `mapping.yaml`），选后展示 namespace / release / chart 路径
3. **选连接方式**：
   - **miks-proxy 只读服务号**（推荐）：自动调用 `miks-iam-tool` 生成 6h kubeconfig，全程只读
   - **上传 kubeconfig**：手动提供凭证文件
4. **开始分析** — 进度面板实时展示每个步骤耗时，完成后自动跳转报告页

## 报告页

- 顶部：风险等级徽章 + 组件/集群/时间元信息
- 左侧目录：锚点跳转各节
- 右侧内容：marked.js 渲染 markdown，代码块 highlight.js 高亮
- 操作：下载 `.md` / 复制全文
- 历史报告：首页导航 → 历史报告，列出所有 `helm-change-report-*.md`

## 架构

```
web/
├── server/
│   ├── app.py              FastAPI 入口，SSE 流式推送进度
│   ├── config_index.py     启动时扫描 miks-config，构建集群/组件索引
│   ├── helm_runner.py      subprocess 调 helm template，支持多 chart 组件
│   ├── cluster_fetcher.py  kubectl 拉取集群资源（RBAC 错误单独报告，不误判为新增）
│   ├── differ.py           语义级 yaml diff（按 kind+namespace+name 分组）
│   ├── ai_analyzer.py      调 Anthropic 兼容网关，重试 429/5xx
│   ├── report_writer.py    5 节 markdown 报告
│   └── prompts.py          AI 风险评估 system prompt
├── web/
│   ├── index.html          主页（Alpine.js，无打包工具）
│   ├── report.html         报告页
│   ├── app.js              前端逻辑
│   └── styles.css
├── .env.example
├── requirements.txt
└── run.py                  uvicorn 入口（reload=True）
```

## Diff 逻辑说明

工具实现了**语义级 YAML diff**，而非简单文本对比，以减少噪音：

- 以 `(kind, namespace, name)` 为 key 匹配资源，避免 namespace 缺失导致的误判
- 自动过滤 k8s 运行时注入的字段（不在 helm template 输出中，对比无意义）：
  - 元数据：`resourceVersion` / `uid` / `creationTimestamp` / `managedFields` 等
  - 注解：`kubectl.kubernetes.io/last-applied-configuration` / helm release 注解
  - 标签：`helm.sh/chart`（含版本号）/ `app.kubernetes.io/version` / `app.kubernetes.io/managed-by`
  - Service：`clusterIP` / `clusterIPs` / `ipFamilies` / `sessionAffinity` 等
  - Deployment：`progressDeadlineSeconds` / `revisionHistoryLimit` / 默认 `RollingUpdate` strategy
  - Pod template：`dnsPolicy` / `restartPolicy` / `schedulerName` / `terminationGracePeriodSeconds` 等
  - 容器：`terminationMessagePath` / `terminationMessagePolicy` / 空 `resources`/`securityContext`
  - k8s 自动注入的 service account token volume（`kube-api-access-*`）
- 对 RBAC 无权读取或 CRD 未注册的资源，单独在报告中说明「无法读取」，不误报为「新增」

## 安全说明

- 服务仅绑定 `127.0.0.1:8080`，不对外暴露
- kubectl 命令硬编码白名单（仅 `get` / `describe` / `version`），任何写操作关键词直接报错拒绝
- 上传的 kubeconfig 存入 `/tmp/helm-change-agent/<run_id>/`，随系统 `/tmp` 清理
- `.env` 已加入 `.gitignore`，不会提交到仓库

## 报告存储

报告写入 `REPORTS_DIR/helm-change-report-<组件>-<集群>.md`，与 `helm-change-review` skill 生成的历史报告命名约定一致，两者可共存于同一目录互相查阅。
