# python-repl-mcp

一个基于 Model Context Protocol (MCP) 的 Python REPL 服务器，支持多会话管理、代码执行、历史记录回放和包安装。给 AI 装上一个 Python 解释器，配合任意 Python 库即可扩展无限能力。

## 功能特性

- **多会话管理** - 创建、列出、重置、删除独立的 Python 执行会话
- **进程级隔离** - 每个会话运行在独立的 worker 进程中，互不干扰
- **代码执行** - 在指定会话中执行 Python 代码，自动捕获 stdout/stderr
- **文件执行** - 直接在会话中执行 Python 文件
- **表达式求值** - 自动检测最后一条语句是否为表达式并返回其值（类似 IPython）
- **超时控制** - 可设置 timeout 参数限制执行时间，超时自动终止并重启 worker
- **执行历史** - 带编号的代码块格式输出，支持 Python 切片索引
- **历史回放** - 重置运行时并从历史中重新执行代码
- **历史导出** - 将完整执行记录（代码+输出）导出到文件
- **脚本导出** - 将历史代码（仅代码）保存为 .py 文件
- **运行时重置** - 终止 worker 进程并启动新进程，保留历史记录
- **包安装** - 通过 pip 安装 Python 包
- **多通信方式** - 通过环境变量切换 stdio / SSE / Streamable HTTP 传输

## 安装

```bash
pip install python-repl-mcp
```

## MCP 配置

在 `.kiro/settings/mcp.json` 或 `~/.kiro/settings/mcp.json` 中添加：

### 使用 Python 启动（推荐）

直接运行在当前 Python 环境中，可以访问本机已安装的所有包。

```json
{
  "mcpServers": {
    "python-repl": {
      "command": "python",
      "args": ["-m", "python_repl_mcp"],
      "disabled": false
    }
  }
}
```

### 使用 uvx 启动

无需预先安装，`uvx` 会自动下载并运行。但注意 `uvx` 会创建临时隔离的虚拟环境，本机已安装的库在其中不可用，需通过 `install_package` 工具重新安装。

```json
{
  "mcpServers": {
    "python-repl": {
      "command": "uvx",
      "args": ["python-repl-mcp"],
      "disabled": false
    }
  }
}
```

## 环境变量

| 变量名 | 说明 | 可选值 | 默认值 |
|--------|------|--------|--------|
| `MCP_TRANSPORT` | MCP 通信方式 | `stdio`, `sse`, `streamable-http` | `stdio` |
| `MCP_HOST` | HTTP 模式绑定地址 | 任意 IP 地址 | `127.0.0.1` |
| `MCP_PORT` | HTTP 模式绑定端口 | 任意端口号 | `8000` |

> `MCP_HOST` 和 `MCP_PORT` 仅在 `MCP_TRANSPORT` 为 `sse` 或 `streamable-http` 时生效。

### 示例：使用 SSE 模式

```json
{
  "mcpServers": {
    "python-repl": {
      "command": "python",
      "args": ["-m", "python_repl_mcp"],
      "env": {
        "MCP_TRANSPORT": "sse",
        "MCP_HOST": "0.0.0.0",
        "MCP_PORT": "8080"
      },
      "disabled": false
    }
  }
}
```

## 索引规则

所有接受 `start`/`end` 参数的工具，均遵循 **标准 Python 切片** 约定：

- **0-based** — 第一个代码块索引为 0
- **半开区间 `[start, end)`** — start 包含，end 不包含
- **负数索引** — `-1` 表示最后一个，`-2` 表示倒数第二个
- **None 默认值** — `start=None` 从头开始，`end=None` 到末尾
- **越界自动截断** — 不会报错，同 Python 切片行为

示例（假设有 5 个代码块）：

| 参数 | 效果 | 等价 Python |
|------|------|------------|
| `start=0, end=2` | 前 2 个块 | `lst[0:2]` |
| `start=-1` | 最后 1 个块 | `lst[-1:]` |
| `start=-2` | 最后 2 个块 | `lst[-2:]` |
| `start=1, end=-1` | 去掉首尾 | `lst[1:-1]` |
| 不传参数 | 全部 | `lst[:]` |

> 注意：输出中显示的编号 `[1]`, `[2]`, `[3]` 是 1-based 的人类可读编号，API 参数使用 0-based 索引。

## 工具列表

### `create_session`
创建一个新的 Python REPL 会话。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 否 | 自定义会话ID，不提供则自动生成 |

### `list_sessions`
列出所有活跃的会话及其元数据（session_id、created_at、history_count、alive）。

### `reset_session`
重置会话的命名空间和执行历史，会话本身保留。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 要重置的会话ID |

### `reset_run_context`
终止当前 worker 进程并启动新进程，提供全新的 Python 解释器环境。历史记录保留。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 要重置的会话ID |

### `delete_session`
永久删除一个会话及其所有数据。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 要删除的会话ID |

### `run_code`
在指定会话中执行 Python 代码。支持两种模式：直接提供代码，或从历史记录中提取代码执行。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 执行代码的会话ID |
| code | string | 否 | 要执行的 Python 代码（不提供则从历史中提取） |
| start | integer | 否 | 起始索引（Python 切片规则） |
| end | integer | 否 | 结束索引（Python 切片规则） |
| timeout | integer | 否 | 执行超时秒数，默认不超时 |

### `run_file`
在指定会话中执行一个 Python 文件。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 执行文件的会话ID |
| path | string | 是 | Python 文件路径 |
| timeout | integer | 否 | 执行超时秒数，默认不超时 |

### `rerun_code`
重置运行时环境，从历史记录中重新执行代码。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 会话ID |
| start | integer | 否 | 起始索引（Python 切片规则） |
| end | integer | 否 | 结束索引（Python 切片规则） |
| timeout | integer | 否 | 每个代码块的超时秒数 |

### `get_history`
获取会话的执行历史。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 要查看的会话ID |
| start | integer | 否 | 起始索引（Python 切片规则） |
| end | integer | 否 | 结束索引（Python 切片规则） |

输出示例：
```
[1] a = 1
[2] a
1
[3] b
Traceback (most recent call last):
  File "<stdin>", line 1, in <module>
NameError: name 'b' is not defined
```

### `delete_history`
删除指定范围的历史记录。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 会话ID |
| start | integer | 是 | 起始索引（Python 切片规则） |
| end | integer | 否 | 结束索引，不传则删除 start 处的单个代码块 |

### `export_history`
将执行历史（代码+输出）导出到文件。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 会话ID |
| path | string | 是 | 保存路径（如 'history.txt'） |
| start | integer | 否 | 起始索引（Python 切片规则） |
| end | integer | 否 | 结束索引（Python 切片规则） |

### `save_script`
将历史代码（仅代码，不含输出）导出为 Python 脚本文件。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 会话ID |
| path | string | 是 | 保存路径（如 'output.py'） |
| start | integer | 否 | 起始索引（Python 切片规则） |
| end | integer | 否 | 结束索引（Python 切片规则） |

### `install_package`
通过 pip 安装 Python 包到当前环境，安装后所有会话可用。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| package_name | string | 是 | 包名（如 'numpy', 'pandas==2.0.0'） |

## 使用示例

```
1. create_session(session_id="demo")

2. run_code(session_id="demo", code="import math\nresult = math.sqrt(144)\nresult")
   → [1] import math
         result = math.sqrt(144)
         result
     12.0

3. run_code(session_id="demo", code="result + 1")
   → [2] result + 1
     13.0

4. run_file(session_id="demo", path="/my/project/utils.py")
   → [3] exec('utils.py')
     Loaded 5 utility functions.

5. get_history(session_id="demo")
   → [1] import math
         result = math.sqrt(144)
         result
     12.0

     [2] result + 1
     13.0

     [3] exec('utils.py')
     Loaded 5 utility functions.

6. get_history(session_id="demo", start=-1)
   → [3] exec('utils.py')
     Loaded 5 utility functions.

7. export_history(session_id="demo", path="history.txt")
   → History exported to 'history.txt'

8. save_script(session_id="demo", path="demo.py")
   → Script saved to 'demo.py'

9. reset_run_context(session_id="demo")
10. delete_session(session_id="demo")
```

## 项目结构

```
python-repl-mcp/
├── pyproject.toml
├── README.md
└── python_repl_mcp/
    ├── __init__.py
    ├── __main__.py
    ├── server.py      # MCP 服务器：会话管理、工具定义
    └── worker.py      # Worker 进程：代码执行引擎
```

## 开发

```bash
# 克隆项目
git clone https://github.com/miloira/python-repl-mcp.git
cd python-repl-mcp

# 开发模式安装
pip install -e .

# 运行服务器
python -m python_repl_mcp
```

## 发布

```bash
pip install build twine
python -m build
twine upload dist/*
```

## 设计理念

相比传统的固定 Skill/Tool，python-repl-mcp 的优势在于：

- **万能扩展** — `pip install` 任意库 + 丢一份文档 = 新能力上线
- **零开发成本** — 不需要写 tool schema，Python 能做的事它都能做
- **有状态交互** — 变量持久化，像真的在写代码一样逐步探索
- **灵活组合** — 一段代码搞定复杂编排，无需多个 tool 串联
