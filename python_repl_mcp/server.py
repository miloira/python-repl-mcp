"""MCP Server for Python REPL - sessions, execution, and tool definitions."""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Thread-local stdout/stderr proxy for concurrent output isolation
# ---------------------------------------------------------------------------

_thread_local = threading.local()


class _ThreadLocalStream:
    """A stream proxy that routes writes to a thread-local buffer when set,
    otherwise falls through to the original stream.

    This allows concurrent code executions in different threads to capture
    their own stdout/stderr independently without interfering with each other.
    """

    def __init__(self, original: Any, attr_name: str) -> None:
        self._original = original
        self._attr_name = attr_name  # e.g. "stdout" or "stderr"

    def write(self, data: str) -> int:
        stream = getattr(_thread_local, self._attr_name, None)
        if stream is not None:
            return stream.write(data)
        return self._original.write(data)

    def flush(self) -> None:
        stream = getattr(_thread_local, self._attr_name, None)
        if stream is not None:
            stream.flush()
        else:
            self._original.flush()

    def fileno(self) -> int:
        return self._original.fileno()

    def isatty(self) -> bool:
        return self._original.isatty()

    @property
    def encoding(self) -> str:
        return self._original.encoding

    def __getattr__(self, name: str) -> Any:
        # Delegate any other attribute access to the original stream
        return getattr(self._original, name)


# Install thread-local stream proxies at module load time.
# After this, any thread can set _thread_local.stdout / _thread_local.stderr
# to a StringIO to capture its own output without affecting other threads.
_original_stdout = sys.stdout
_original_stderr = sys.stderr
sys.stdout = _ThreadLocalStream(_original_stdout, "stdout")  # type: ignore[assignment]
sys.stderr = _ThreadLocalStream(_original_stderr, "stderr")  # type: ignore[assignment]

# Global lock for process-wide state: cwd changes and sys.path modifications
_cwd_lock = threading.Lock()
_path_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = None  # None means no timeout


@dataclass
class ExecutionRecord:
    """A record of a single code execution."""

    code: str
    output: str
    error: str
    timestamp: float = field(default_factory=time.time)
    success: bool = True
    record_type: str = "python"  # "python" for code, "system" for markers


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """A Python REPL session with its own namespace and execution history."""

    session_id: str
    cwd: str | None = None
    sys_paths: list[str] = field(default_factory=list)
    namespace: dict[str, Any] = field(default_factory=dict)
    history: list[ExecutionRecord] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    _initial_modules: set[str] = field(default_factory=set, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Snapshot initial interpreter state at creation time."""
        if not self._initial_modules:
            self._initial_modules = set(sys.modules.keys())

    def reset(self) -> None:
        """Reset the session namespace and history."""
        self.namespace.clear()
        self.history.clear()

    def reset_run_context(self) -> None:
        """Recreate a fresh Python interpreter execution environment.

        This goes beyond a simple reset by also:
        - Removing any modules imported during the session from sys.modules
        - Restoring sys.path to its state before session-specific additions
        - Creating a completely clean namespace (as if a new interpreter started)
        - Appending a reset marker to history (history is preserved)
        """
        # Remove modules that were imported during this session
        current_modules = set(sys.modules.keys())
        session_modules = current_modules - self._initial_modules
        for mod_name in session_modules:
            try:
                del sys.modules[mod_name]
            except KeyError:
                pass

        # Remove session-specific sys.path entries
        for p in self.sys_paths:
            try:
                sys.path.remove(p)
            except ValueError:
                pass
        if self.cwd and self.cwd in sys.path:
            try:
                sys.path.remove(self.cwd)
            except ValueError:
                pass

        # Re-add session paths (fresh start, same config)
        for p in self.sys_paths:
            if p not in sys.path:
                sys.path.insert(0, p)
        if self.cwd and self.cwd not in sys.path:
            sys.path.insert(0, self.cwd)

        # Clear namespace only, keep history
        self.namespace.clear()

        # Append a reset marker to history
        self.history.append(ExecutionRecord(
            code="--- runtime context reset ---",
            output="",
            error="",
            timestamp=time.time(),
            success=True,
            record_type="system",
        ))

        # Re-snapshot modules for the new clean state
        self._initial_modules = set(sys.modules.keys())

    def get_variables(self) -> dict[str, str]:
        """Get all user-defined variables with their type and repr."""
        variables: dict[str, str] = {}
        for name, value in self.namespace.items():
            if name.startswith("_"):
                continue
            try:
                variables[name] = f"{type(value).__name__}: {repr(value)}"
            except Exception:
                variables[name] = f"{type(value).__name__}: <unable to repr>"
        return variables

    def get_history(self, n: int | None = None) -> list[dict[str, Any]]:
        """Get execution history as structured data."""
        if n is None:
            start = 0
        else:
            start = max(0, len(self.history) - n)
        return [
            {
                "index": start + i,
                "code": r.code,
                "output": r.output,
                "error": r.error,
                "success": r.success,
                "timestamp": r.timestamp,
                "record_type": r.record_type,
            }
            for i, r in enumerate(self.history[start:])
        ]

    def format_history(self, n: int | None = None) -> str:
        """Format execution history like a Python interactive REPL with line numbers.

        Output style:
            [1] >>> a = 1
            [2] >>> a
            1
            [3] [system] --- runtime context reset ---
            [4] >>> b
            Traceback (most recent call last):
              File "<stdin>", line 1, in <module>
            NameError: name 'b' is not defined
        """
        records = self.history if n is None else self.history[-n:]
        parts: list[str] = []
        # Determine starting line number (1-based, all records count)
        if n is None or n >= len(self.history):
            line_num = 1
        else:
            line_num = len(self.history) - n + 1

        for record in records:
            if record.record_type == "system":
                parts.append(f"[{line_num}] [system] {record.code}")
                parts.append("")
                line_num += 1
                continue

            lines = record.code.splitlines()
            for i, line in enumerate(lines):
                if i == 0:
                    parts.append(f"[{line_num}] >>> {line}")
                else:
                    parts.append(f"{'':>{len(str(line_num)) + 2}} ... {line}")

            if record.success:
                if record.output:
                    parts.append(record.output)
            else:
                if record.error:
                    parts.append(record.error)

            parts.append("")
            line_num += 1

        if parts and parts[-1] == "":
            parts.pop()

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Session manager
# ---------------------------------------------------------------------------


class SessionManager:
    """Manages multiple Python REPL sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        session_id: str | None = None,
        cwd: str | None = None,
        sys_paths: list[str] | None = None,
    ) -> Session:
        """Create a new session."""
        if session_id is None:
            session_id = str(uuid.uuid4())[:8]

        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session '{session_id}' already exists")

            if cwd is not None:
                cwd = os.path.abspath(cwd)
                if not os.path.isdir(cwd):
                    raise ValueError(f"Working directory '{cwd}' does not exist")

            resolved_paths: list[str] = []
            if sys_paths:
                for p in sys_paths:
                    abs_path = os.path.abspath(p)
                    resolved_paths.append(abs_path)

            with _path_lock:
                if sys_paths:
                    for abs_path in resolved_paths:
                        if abs_path not in sys.path:
                            sys.path.insert(0, abs_path)
                if cwd and cwd not in sys.path:
                    sys.path.insert(0, cwd)

            session = Session(session_id=session_id, cwd=cwd, sys_paths=resolved_paths)
            self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> Session:
        """Get an existing session by ID."""
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session '{session_id}' not found")
            return self._sessions[session_id]

    def delete_session(self, session_id: str) -> None:
        """Delete a session."""
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session '{session_id}' not found")
            del self._sessions[session_id]

    def reset_session(self, session_id: str) -> None:
        """Reset a session's namespace and history."""
        session = self.get_session(session_id)
        with session._lock:
            session.reset()

    def reset_run_context(self, session_id: str) -> None:
        """Recreate a fresh Python interpreter environment for the session."""
        session = self.get_session(session_id)
        with session._lock:
            session.reset_run_context()

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions with metadata."""
        with self._lock:
            return [
                {
                    "session_id": s.session_id,
                    "cwd": s.cwd,
                    "sys_paths": s.sys_paths,
                    "created_at": s.created_at,
                    "history_count": len(s.history),
                    "variable_count": len(s.get_variables()),
                }
                for s in self._sessions.values()
            ]


# ---------------------------------------------------------------------------
# Code executor
# ---------------------------------------------------------------------------


def execute_code(session: Session, code: str, timeout: int | None = None) -> ExecutionRecord:
    """Execute Python code within a session's namespace.

    Handles both expressions (returns value) and statements.
    Captures stdout/stderr via thread-local streams. Uses a thread with timeout.
    Acquires the session lock to prevent concurrent executions on the same session.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT

    # None means no timeout (thread.join with None waits forever)

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    exec_result: dict[str, Any] = {
        "output": "",
        "error": "",
        "success": True,
        "finished": False,
    }

    def _run():
        # Install thread-local capture streams
        _thread_local.stdout = stdout_capture
        _thread_local.stderr = stderr_capture
        try:
            # Acquire the cwd lock and change directory if needed
            if session.cwd:
                _cwd_lock.acquire()
                old_cwd = os.getcwd()
                os.chdir(session.cwd)

            try:
                result = _try_exec(code, session.namespace)

                out = stdout_capture.getvalue()
                if result is not None:
                    if out:
                        out += "\n"
                    out += repr(result)

                exec_result["output"] = out
                exec_result["success"] = True

            except Exception as exc:
                exec_result["output"] = stdout_capture.getvalue()
                exec_result["error"] = _format_repl_error(exc)
                exec_result["success"] = False
            finally:
                if session.cwd:
                    try:
                        os.chdir(old_cwd)
                    except Exception:
                        pass
                    _cwd_lock.release()
        finally:
            # Remove thread-local captures
            _thread_local.stdout = None
            _thread_local.stderr = None
            exec_result["finished"] = True

    with session._lock:
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if not exec_result["finished"]:
            success = False
            output = stdout_capture.getvalue()
            error = f"ExecutionTimeout: code execution exceeded {timeout} seconds"
        else:
            output = exec_result["output"]
            error = exec_result["error"]
            success = exec_result["success"]

        stderr_output = stderr_capture.getvalue()
        if stderr_output and stderr_output not in error:
            if error:
                error += "\n"
            error += stderr_output

        record = ExecutionRecord(
            code=code,
            output=output.rstrip() if output else "",
            error=error.rstrip() if error else "",
            timestamp=time.time(),
            success=success,
        )
        session.history.append(record)
        return record


def _format_repl_error(exc: BaseException) -> str:
    """Format an exception like the Python interactive REPL."""
    tb = traceback.extract_tb(exc.__traceback__)
    repl_frames = [frame for frame in tb if frame.filename == "<repl>"]

    lines = ["Traceback (most recent call last):"]
    if repl_frames:
        for frame in repl_frames:
            lines.append(f'  File "<stdin>", line {frame.lineno}, in {frame.name}')
            if frame.line:
                lines.append(f"    {frame.line}")
    else:
        lines.append('  File "<stdin>", line 1, in <module>')

    exc_line = traceback.format_exception_only(type(exc), exc)
    lines.extend(line.rstrip() for line in exc_line)

    return "\n".join(lines)


def _try_exec(code: str, namespace: dict) -> object | None:
    """Execute code, returning the result if it's a single expression."""
    tree = ast.parse(code)

    if not tree.body:
        return None

    last = tree.body[-1]

    if isinstance(last, ast.Expr):
        if len(tree.body) > 1:
            module = ast.Module(body=tree.body[:-1], type_ignores=[])
            exec(compile(module, "<repl>", "exec"), namespace)

        expr = ast.Expression(body=last.value)
        return eval(compile(expr, "<repl>", "eval"), namespace)
    else:
        exec(compile(tree, "<repl>", "exec"), namespace)
        return None


# ---------------------------------------------------------------------------
# Command execution utility
# ---------------------------------------------------------------------------


def cmd(args, input_values=None, on_input=None, encoding="gbk", chunk_size=1024, **kwargs):
    """Execute a command with streaming output and optional interactive input.

    Args:
        args: Command and arguments (list or string).
        input_values: Dict mapping output prompts (bytes) to input responses (bytes).
        on_input: Callable that receives the last output line (bytes) and returns
            input bytes or None.
        encoding: Output decoding encoding. Default 'gbk' for Windows.
        chunk_size: Read chunk size in bytes.
        **kwargs: Additional arguments passed to subprocess.Popen.

    Returns:
        CmdResult object that is iterable (yields decoded output chunks)
        and has .return_code and .output attributes after iteration completes.
    """
    if input_values is not None and not isinstance(input_values, dict):
        raise TypeError("input_values must be a dict.")

    if on_input is not None and not callable(on_input):
        raise TypeError("on_input must be callable.")

    input_values = {} if input_values is None else input_values

    class CmdResult:
        def __init__(self, gen):
            self.gen = gen
            self.return_code = None
            self.output = None

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.gen)

        def __str__(self):
            return f"<CmdResult return_code={self.return_code} output={bytes(self.output)}>"

    def generator():
        with subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                text=False,
                **kwargs
        ) as process:
            output = bytearray()
            while True:
                chunk = process.stdout.read(chunk_size)
                if not chunk:
                    break
                chunk = cast(bytes, chunk)
                yield chunk.decode(encoding)
                output.extend(chunk)
                output_lines = output.splitlines(keepends=True)
                last_line = bytes(output_lines[-1]).rstrip(b"\r\n")
                input_value = None
                if last_line in input_values:
                    input_value = input_values[last_line]
                if on_input is not None:
                    input_value = on_input(last_line)
                if input_value is not None:
                    input_value += b"\n"
                    yield input_value.decode(encoding)
                    output.extend(input_value)
                    process.stdin.write(input_value)
                    process.stdin.flush()

        cmd_result.return_code = process.returncode
        cmd_result.output = output

    cmd_result = CmdResult(generator())
    return cmd_result


# ---------------------------------------------------------------------------
# MCP Server & Tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "python-repl",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)

manager = SessionManager()


@mcp.tool()
def create_session(
    session_id: str | None = None,
    cwd: str | None = None,
    sys_paths: list[str] | None = None,
) -> str:
    """Create a new Python REPL session.

    Each session has its own isolated namespace for variable storage
    and its own execution history.

    Args:
        session_id: Optional custom session ID. Auto-generated if not provided.
        cwd: Optional working directory for the session. Code execution will
            use this as the current directory, and it's also added to sys.path.
        sys_paths: Optional list of additional paths to add to sys.path,
            allowing imports from those directories.

    Returns:
        JSON with the created session info.
    """
    try:
        session = manager.create_session(session_id, cwd=cwd, sys_paths=sys_paths)
        result = {
            "status": "success",
            "session_id": session.session_id,
            "message": f"Session '{session.session_id}' created successfully",
        }
        if session.cwd:
            result["cwd"] = session.cwd
        if session.sys_paths:
            result["sys_paths"] = session.sys_paths
        return json.dumps(result)
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def list_sessions() -> str:
    """List all active Python REPL sessions.

    Returns:
        JSON with a list of all sessions and their metadata.
    """
    sessions = manager.list_sessions()
    return json.dumps({
        "status": "success",
        "sessions": sessions,
        "total": len(sessions),
    })


@mcp.tool()
def reset_session(session_id: str) -> str:
    """Reset a session, clearing its namespace and execution history.

    The session will remain active but with a fresh state.

    Args:
        session_id: The ID of the session to reset.

    Returns:
        JSON with the operation result.
    """
    try:
        manager.reset_session(session_id)
        return json.dumps({
            "status": "success",
            "message": f"Session '{session_id}' has been reset",
        })
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def reset_run_context(session_id: str) -> str:
    """Recreate a fresh Python interpreter execution environment for a session.

    Unlike reset_session which only clears variables and history, this fully
    recreates the runtime context: unloads session-imported modules from
    sys.modules, resets sys.path modifications, and provides a completely
    clean namespace as if a new Python interpreter was started.

    The session keeps its configuration (cwd, sys_paths) but all runtime
    state is discarded.

    Args:
        session_id: The ID of the session to reset.

    Returns:
        JSON with the operation result.
    """
    try:
        manager.reset_run_context(session_id)
        return json.dumps({
            "status": "success",
            "message": f"Session '{session_id}' runtime context has been recreated",
        })
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def delete_session(session_id: str) -> str:
    """Delete a session permanently.

    Removes the session and all its data (namespace, history).

    Args:
        session_id: The ID of the session to delete.

    Returns:
        JSON with the operation result.
    """
    try:
        manager.delete_session(session_id)
        return json.dumps({
            "status": "success",
            "message": f"Session '{session_id}' has been deleted",
        })
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})


@mcp.tool()
def run_code(
    session_id: str,
    code: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    timeout: int | None = None,
) -> str:
    """Execute Python code in the specified session.

    The code runs within the session's namespace, so variables defined
    in previous executions are accessible. If the last statement is an
    expression, its value will be returned.

    There are two modes:
    1. Provide code directly (optionally sliced by start_line/end_line).
    2. Omit code and use start_line/end_line to index into the session's
       history, concatenating the code from those history records (skipping
       system records) and executing the result.

    Line numbers are 1-based and inclusive.

    Args:
        session_id: The session to execute code in.
        code: The Python code to execute. If omitted, code is assembled from history.
        start_line: 1-based start index. When code is provided, slices code lines.
            When code is omitted, indexes into history records.
        end_line: 1-based end index (inclusive). Same semantics as start_line.
        timeout: Execution timeout in seconds. Default is None (no timeout).
            Set a positive value to limit execution time.

    Returns:
        The execution result formatted as interactive Python REPL output.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    if code:
        # Slice provided code by line range
        if start_line is not None or end_line is not None:
            all_lines = code.splitlines()
            total = len(all_lines)
            s = (start_line - 1) if start_line and start_line >= 1 else 0
            e = end_line if end_line and end_line <= total else total
            if s >= total or s < 0 or e < 1 or s >= e:
                return json.dumps({
                    "status": "error",
                    "message": f"Invalid line range: start_line={start_line}, end_line={end_line} (code has {total} lines)",
                })
            code = "\n".join(all_lines[s:e])
    else:
        # Assemble code from history records using start_line/end_line as record indices
        if start_line is None and end_line is None:
            return json.dumps({"status": "error", "message": "No code provided"})

        total = len(session.history)
        s = (start_line - 1) if start_line and start_line >= 1 else 0
        e = end_line if end_line and end_line <= total else total
        if s >= total or s < 0 or e < 1 or s >= e:
            return json.dumps({
                "status": "error",
                "message": f"Invalid range: start_line={start_line}, end_line={end_line} (history has {total} records)",
            })

        code_parts: list[str] = []
        for record in session.history[s:e]:
            if record.record_type == "system":
                continue
            code_parts.append(record.code)

        if not code_parts:
            return json.dumps({"status": "error", "message": "No executable code in the specified history range"})

        code = "\n".join(code_parts)

    record = execute_code(session, code, timeout=timeout)

    # Line number = total history records count (this record is the latest)
    line_num = len(session.history)

    parts: list[str] = []
    lines = code.splitlines()
    for i, line in enumerate(lines):
        if i == 0:
            parts.append(f"[{line_num}] >>> {line}")
        else:
            parts.append(f"{'':>{len(str(line_num)) + 2}} ... {line}")

    if record.success:
        if record.output:
            parts.append(record.output)
    else:
        if record.error:
            parts.append(record.error)

    return "\n".join(parts)


@mcp.tool()
def get_history(session_id: str, n: int | None = None) -> str:
    """Get the execution history of a session, formatted like a Python interactive REPL.

    Output looks like:
        [1] >>> a = 1
        [2] >>> a
        1
        [3] >>> b
        Traceback (most recent call last):
          File "<stdin>", line 1, in <module>
        NameError: name 'b' is not defined

    Args:
        session_id: The session to get history from.
        n: Number of recent entries to return. None or 0 means all history.

    Returns:
        The execution history formatted as interactive Python REPL output
        with line numbers for reference in run_code and delete_history.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    count = n if n and n > 0 else None
    formatted = session.format_history(count)
    return formatted


@mcp.tool()
def install_package(package_name: str) -> str:
    """Install a Python package using pip.

    The package is installed into the current Python environment
    and becomes available to all sessions.

    Args:
        package_name: The package to install (e.g. 'numpy', 'pandas==2.0.0').

    Returns:
        JSON with the installation result.
    """
    try:
        result = cmd([sys.executable, "-m", "pip", "install", package_name])
        output_parts: list[str] = []
        for chunk in result:
            output_parts.append(chunk)
        output = "".join(output_parts)

        if result.return_code == 0:
            return json.dumps({
                "status": "success",
                "message": f"Package '{package_name}' installed successfully",
                "output": output.strip(),
            })
        else:
            return json.dumps({
                "status": "error",
                "message": f"Failed to install '{package_name}'",
                "error": output.strip(),
            })
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Unexpected error: {str(e)}",
        })


@mcp.tool()
def delete_history(
    session_id: str,
    start_line: int,
    end_line: int | None = None,
) -> str:
    """Delete a range of history records from a session.

    Args:
        session_id: The session to delete history from.
        start_line: 1-based start record index (inclusive).
        end_line: 1-based end record index (inclusive). Defaults to start_line
            (delete a single record).

    Returns:
        JSON with the operation result.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    total = len(session.history)
    if total == 0:
        return json.dumps({"status": "error", "message": "No history to delete"})

    if end_line is None:
        end_line = start_line

    s = start_line - 1
    e = end_line

    if s < 0 or e > total or s >= e:
        return json.dumps({
            "status": "error",
            "message": f"Invalid range: start_line={start_line}, end_line={end_line} (history has {total} records)",
        })

    deleted_count = e - s
    del session.history[s:e]

    return json.dumps({
        "status": "success",
        "message": f"Deleted {deleted_count} record(s) from history (lines {start_line}-{end_line})",
        "remaining": len(session.history),
    })


@mcp.tool()
def save_script(
    session_id: str,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Save history code to a Python script file.

    Extracts code from the specified history range (skipping system records),
    concatenates it, and writes to the given file path.

    Args:
        session_id: The session to extract code from.
        path: File path to save the script (e.g. 'output.py').
        start_line: Optional 1-based start record index (inclusive). Default: 1.
        end_line: Optional 1-based end record index (inclusive). Default: last record.

    Returns:
        JSON with the operation result.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    total = len(session.history)
    if total == 0:
        return json.dumps({"status": "error", "message": "No history to save"})

    s = (start_line - 1) if start_line and start_line >= 1 else 0
    e = end_line if end_line and end_line <= total else total

    if s >= total or s < 0 or e < 1 or s >= e:
        return json.dumps({
            "status": "error",
            "message": f"Invalid range: start_line={start_line}, end_line={end_line} (history has {total} records)",
        })

    code_parts: list[str] = []
    for record in session.history[s:e]:
        if record.record_type == "system":
            continue
        code_parts.append(record.code)

    if not code_parts:
        return json.dumps({"status": "error", "message": "No executable code in the specified range"})

    script = "\n\n".join(code_parts) + "\n"

    try:
        abs_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(script)
        return json.dumps({
            "status": "success",
            "message": f"Script saved to '{abs_path}'",
            "path": abs_path,
            "lines_saved": len(code_parts),
        })
    except Exception as ex:
        return json.dumps({"status": "error", "message": f"Failed to write file: {str(ex)}"})


@mcp.tool()
def run_file(
    session_id: str,
    path: str,
    timeout: int | None = None,
) -> str:
    """Execute a Python file in the specified session.

    Reads the file content and executes it within the session's namespace,
    equivalent to running `exec(open(path).read())` in the session.
    The file's parent directory is temporarily added to sys.path during execution.

    Args:
        session_id: The session to execute the file in.
        path: Path to the Python file to execute.
        timeout: Execution timeout in seconds. Default is None (no timeout).

    Returns:
        The execution result formatted as interactive Python REPL output.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return json.dumps({"status": "error", "message": f"File not found: '{abs_path}'"})

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            code = f.read()
    except Exception as ex:
        return json.dumps({"status": "error", "message": f"Failed to read file: {str(ex)}"})

    if not code.strip():
        return json.dumps({"status": "error", "message": "File is empty"})

    # Temporarily add the file's directory to sys.path
    file_dir = os.path.dirname(abs_path)
    path_added = False
    with _path_lock:
        if file_dir not in sys.path:
            sys.path.insert(0, file_dir)
            path_added = True

    # Set __file__ in the session namespace during execution
    old_file = session.namespace.get("__file__")
    session.namespace["__file__"] = abs_path

    try:
        record = execute_code(session, code, timeout=timeout)
    finally:
        # Restore __file__
        if old_file is None:
            session.namespace.pop("__file__", None)
        else:
            session.namespace["__file__"] = old_file
        # Remove temporarily added path
        if path_added:
            with _path_lock:
                if file_dir in sys.path:
                    sys.path.remove(file_dir)

    # Format output
    line_num = len(session.history)
    filename = os.path.basename(abs_path)

    parts: list[str] = [f"[{line_num}] >>> exec('{filename}')"]

    if record.success:
        if record.output:
            parts.append(record.output)
    else:
        if record.error:
            parts.append(record.error)

    return "\n".join(parts)


def main() -> None:
    """Run the MCP server.

    Transport mode is controlled by the MCP_TRANSPORT environment variable:
      - "stdio" (default): Standard input/output transport
      - "sse": Server-Sent Events over HTTP
      - "streamable-http": Streamable HTTP transport

    For HTTP-based transports (sse, streamable-http), additional env vars:
      - MCP_HOST: Host to bind (default "127.0.0.1")
      - MCP_PORT: Port to bind (default 8000)
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "sse", "streamable-http"):
        raise ValueError(
            f"Invalid MCP_TRANSPORT='{transport}'. "
            f"Must be one of: stdio, sse, streamable-http"
        )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
