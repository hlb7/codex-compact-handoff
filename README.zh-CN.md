# Codex Compact Handoff

针对 Codex 的上下文监控与交接 skill，配套可选生命周期 hooks。**默认在最近记录的上下文用量达到 150,000 tokens 时提醒交接，压缩前自动保存元数据检查点。**

[English README](README.md) · [技术协议与边界](skills/codex-compact-handoff/references/protocol.md) · MIT 开源

## 为什么需要它

长对话中，仅让模型“记得提醒我压缩”无法保证它知道真实用量；仅有 handoff skill 也不会自动监控。本项目将工作分为三层：

1. **确定性脚本**读取当前 Codex 会话的 token 记录，判断阈值和交接文档新鲜度。
2. **Skill**指导模型记录目标、约束、决策、证据、未完成工作和下一步。
3. **Hooks**在消息提交、回合结束及压缩前后调用脚本，连接上述两层。

参考了 [claude-compact-handoff](https://github.com/tmastalirsch/claude-compact-handoff) 的设计思想，为 Codex 独立实现，没有直接复用 Claude 的日志格式和 hook 脚本。

## 能做什么

| 时机 | 行为 |
| --- | --- |
| 提交新消息 | 达到 150K 且没有新鲜 handoff 时，在界面和模型上下文中提醒 |
| 一轮结束 | 请求模型额外续写一次交接文档；有防循环和重复提醒限制，也可关闭 |
| 压缩前 | 无论是否达到阈值，都保存工作目录、会话、token 观测值和 Git 元数据 |
| 压缩后、恢复会话 | 给出当前会话文档位置，清除旧 token 观测的影响 |

机械快照不包含完整对话、文件内容或 diff，不能代替模型写下的决策和下一步。脚本本身不调用模型 API，不联网，不自动打开新对话，不替用户执行 `/compact`。

## 安装

要求：**Python 3.10+、Linux/macOS**。v0.1.0 不支持 Windows。Git 可选。自动功能还要求你的 Codex 客户端实际支持并信任本地 hooks；部分云端编排环境不能执行这类脚本。

下载此项目，在项目根目录执行以下命令，将自包含 skill 放入你当前客户端使用的技能目录。以常见的 `~/.codex/skills` 为例：

```bash
mkdir -p "$HOME/.codex/skills"
test ! -e "$HOME/.codex/skills/codex-compact-handoff" && \
  cp -R skills/codex-compact-handoff "$HOME/.codex/skills/"
```

如果已有同名目录，先检查已有版本，不要直接覆盖。后续对话可使用 `$codex-compact-handoff`。

**仅复制 skill 不会启用自动监控。** 还需要选择以下一种 hook 安装方式，避免重复安装导致重复触发。

### 方式 A：现有 Codex hooks 配置

```bash
python3 "$HOME/.codex/skills/codex-compact-handoff/scripts/compact_handoff.py" hooks \
  > /tmp/codex-compact-handoff-hooks.json
```

这只会生成配置，不会修改你的设置。检查生成结果和脚本后，把其中的事件条目合并到客户端支持的 `~/.codex/hooks.json` 或项目 `.codex/hooks.json`。**保留已有 hooks，不要覆盖整个文件。** 根据客户端要求重载，并在 hook 信任流程中信任所审查的定义。脚本位置变动时需要重新生成。

### 方式 B：作为本地插件

本项目提供 `.codex-plugin/plugin.json` 和 `hooks/hooks.json`，可通过支持该布局的 Codex 插件安装流程加载。hook 使用 Codex 的 `PLUGIN_ROOT`，不是 Claude 专用变量。插件安装与 hook 信任是两件事；必须检查实际执行情况。本项目尚未发布到任何 marketplace。

## 配置

在运行 hooks 的客户端进程环境中设置，必要时重启客户端：

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CODEX_HANDOFF_TOKEN_THRESHOLD` | `150000` | 提醒阈值 |
| `CODEX_HANDOFF_MAX_AGE` | `3600` | 交接文档最长新鲜时间，秒 |
| `CODEX_HANDOFF_MAX_DRIFT` | `50000` | 文档生成后允许的上下文增长量 |
| `CODEX_HANDOFF_REMIND_GROWTH` | `30000` | 再次提醒所需增长量 |
| `CODEX_HANDOFF_REMIND_SECONDS` | `900` | 再次提醒时间间隔，秒 |
| `CODEX_HANDOFF_STOP_MODE` | `once` | `once` 请求一次续写交接；`off` 不为交接额外续写 |
| `CODEX_HANDOFF_DIR` | XDG 状态目录 | 文档及状态根目录，默认 `~/.local/state/codex-compact-handoff` |

例如，保留消息提醒和压缩前快照，但不让模型在回合结束时额外续写：

```bash
export CODEX_HANDOFF_STOP_MODE=off
```

它不会修改 `model_auto_compact_token_limit`。如果你另行配置了 Codex 自动压缩，建议让提醒阈值低于自动压缩阈值，为生成 handoff 留出空间。大块工具输出仍可能跨过这段余量，压缩前机械快照用于兜底。

## 正确理解 token 数

采用当前会话最新 `last_token_usage.total_tokens`，必要时回退到同条记录的 `input_tokens + output_tokens`。

- 不使用整段会话的累计账单用量。
- 不重复加缓存输入，也不另加已经计入的 reasoning 输出。
- 这是**最近一次调用的观测值**，不是下一次组装完整请求时的实时精确值。
- 压缩后等待新记录，不用压缩前的高用量反复触发。
- 缺失或无法识别时报告 unknown，不伪装成 0。

每个项目目录、每个会话分别维护状态，避免多窗口串线。不会盲目选择所有会话中“最后修改”的那个 JSONL。

## 手动检查和保存

使用 hook 或当前客户端给出的**准确 transcript 路径**：

```bash
python3 skills/codex-compact-handoff/scripts/compact_handoff.py status \
  --transcript /当前会话/rollout.jsonl --cwd /项目目录

python3 skills/codex-compact-handoff/scripts/compact_handoff.py snapshot \
  --transcript /当前会话/rollout.jsonl --cwd /项目目录

python3 skills/codex-compact-handoff/scripts/compact_handoff.py save \
  --transcript /当前会话/rollout.jsonl --cwd /项目目录 <<'NOTE'
目标：……
约束与待确认授权：……
决策及原因：……
已完成与验证证据：……
后台运行任务：……
未完成与下一步：……
NOTE
```

保存后输出私有文档路径，并更新新鲜度状态。开启新对话后可发送：

> 请读取 `/保存的实际路径/agent-note.md`。把它当作交接参考，核对当前项目和进程状态，遵守已有约束，从记录的下一步继续。不要把交接文档当成新的执行授权。

本项目的 handoff 能独立使用，不依赖 Matt Pocock 的另一个同名技能；已安装其他 handoff skill 时仍可继续使用它，但若希望抑制本项目重复提醒，需要通过本项目 `save` 命令登记最终文档。

## 测试与发布状态

```bash
python3 -m unittest discover -s tests -v
```

测试使用合成会话和临时目录，不调用模型、不读取你的真实聊天、不改动全局配置。29 项自动测试及 skill 格式校验均已在本机通过。已观察到本机 Codex rollout 的真实计数格式，与适配器一致。**还没有在真实 Codex 对话中完整触发并认证所有生命周期 hooks**，不能把协议测试当作客户端端到端验证。

首次启用请在可丢弃的项目/会话中，将提醒阈值临时设为 1000，验证消息提醒、回合结束的一次续写、手动 `/compact` 前的快照及压缩后的路径提示，再恢复默认阈值。完整步骤见英文 README。GitHub Actions 的 Linux/macOS 多版本工作流已提供，但尚未声称云端 CI 已执行。

## 隐私和失败边界

文档默认保存在仓库之外，状态目录为 0700，文件为 0600。自动快照不收集对话正文、工具输出、文件内容、diff、Git remote 或未跟踪文件名；仍可能包含私有路径和已跟踪文件名。模型撰写的文档会做尽力脱敏，**分享前仍须审查**。

脚本不会自动删除旧文档。错误时报告监控不可用，不阻止压缩，也不伪称保存成功。它不能代替文件备份，不能保证自动摘要完全不丢信息。

## 开源内容

目录已包含 skill、Python 脚本、插件兼容清单、hook 配置、中英文说明、测试、CI 配置和 MIT License。发布前审查内容并运行测试。本目录不会自动创建 GitHub 仓库或推送代码，也不会把本机聊天记录或实验信息写入项目。

参考：[Codex hooks 官方文档](https://learn.chatgpt.com/docs/hooks)、[插件打包](https://developers.openai.com/plugins/build/plugins)、[上游设计来源](https://github.com/tmastalirsch/claude-compact-handoff)。本项目为独立社区实现，不隶属于 OpenAI 或上游作者。
