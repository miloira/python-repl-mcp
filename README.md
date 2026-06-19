# python-repl-mcp

一个基于 Model Context Protocol (MCP) 的 Python REPL 服务器，支持多会话管理、代码执行、历史记录回放和包安装。给 AI 装上一个 Python 解释器，配合任意 Python 库即可扩展无限能力。

## 功能特性

- **多会话管理** - 创建、列出、重置、删除独立的 Python 执行会话
- **代码执行** - 在指定会话中执行 Python 代码，自动捕获 stdout/stderr
- **表达式求值** - 自动检测最后一条语句是否为表达式并返回其值（类似 IPython）
- **超时控制** - 代码执行默认不超时，可设置 timeout 参数限制执行时间
- **工作目录** - 创建会话时可指定 cwd 和 sys_paths
- **执行历史** - 交互式 REPL 风格输出（`>>>` 格式），支持查看最近 N 条或全部
- **历史回放** - 从历史记录中提取代码重新执行
- **脚本导出** - 将历史代码保存为 .py 文件
- **运行时重置** - 深度重置会话（卸载导入的模块、恢复 sys.path）
- **包安装** - 通过 pip 安装 Python 包

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

## 工具列表

### `create_session`
创建一个新的 Python REPL 会话。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 否 | 自定义会话ID，不提供则自动生成 |
| cwd | string | 否 | 工作目录，执行代码时自动切换，同时加入 sys.path |
| sys_paths | list[string] | 否 | 额外的包搜索路径列表 |

### `list_sessions`
列出所有活跃的会话及其元数据，包括 cwd、sys_paths、history_count、variable_count 等。

### `reset_session`
重置会话的命名空间和执行历史，会话本身保留。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 要重置的会话ID |

### `reset_run_context`
深度重置：卸载会话中导入的模块、恢复 sys.path、清空命名空间（保留历史记录）。

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
| start_line | integer | 否 | 起始行号（1-based），用于切片代码或索引历史 |
| end_line | integer | 否 | 结束行号（1-based, inclusive） |
| timeout | integer | 否 | 执行超时秒数，默认 None（不超时），设置正数限制执行时间 |

### `get_history`
获取会话的执行历史，以交互式 Python REPL 格式输出。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 要查看的会话ID |
| n | integer | 否 | 返回最近 N 条记录，不传则返回全部 |

输出示例：
```
[1] >>> a = 1
[2] >>> a
1
[3] >>> b
Traceback (most recent call last):
  File "<stdin>", line 1, in <module>
NameError: name 'b' is not defined
```

### `delete_history`
删除指定范围的历史记录。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 会话ID |
| start_line | integer | 是 | 起始记录索引（1-based, inclusive） |
| end_line | integer | 否 | 结束记录索引（1-based, inclusive），默认等于 start_line |

### `save_script`
将历史代码导出为 Python 脚本文件。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 会话ID |
| path | string | 是 | 保存路径（如 'output.py'） |
| start_line | integer | 否 | 起始记录索引，默认 1 |
| end_line | integer | 否 | 结束记录索引，默认最后一条 |

### `install_package`
通过 pip 安装 Python 包到当前环境，安装后所有会话可用。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| package_name | string | 是 | 包名（如 'numpy', 'pandas==2.0.0'） |

## 使用示例

```
1. create_session(session_id="demo", cwd="/my/project")
2. run_code(session_id="demo", code="import math\nresult = math.sqrt(144)\nresult")
   → [1] >>> import math
          ... result = math.sqrt(144)
          ... result
     12.0

3. run_code(session_id="demo", code="result + 1")
   → [2] >>> result + 1
     13.0

4. get_history(session_id="demo")
   → [1] >>> import math
          ... result = math.sqrt(144)
          ... result
     12.0

     [2] >>> result + 1
     13.0

5. save_script(session_id="demo", path="demo.py")
   → Script saved to 'demo.py'

6. reset_run_context(session_id="demo")  # 深度重置，保留历史
7. delete_session(session_id="demo")
```

## 项目结构

```
python-repl-mcp/
├── pyproject.toml
├── README.md
└── python_repl_mcp/
    ├── __init__.py
    └── server.py      # 单文件实现：会话管理、代码执行、MCP工具
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
