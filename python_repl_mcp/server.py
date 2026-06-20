"""MCP Server for Python REPL - sessions with process-level isolation."""

from __future__ import annotations

import ast
import io
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from typing import Any, cast

from mcp.server.fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Worker - runs in a separate process for true isolation
# ---------------------------------------------------------------------------


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


def _execute(code: str, namespace: dict) -> dict[str, Any]:
    """Execute code and capture output."""
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = stdout_capture
    sys.stderr = stderr_capture

    try:
        result = _try_exec(code, namespace)

        out = stdout_capture.getvalue()
        if result is not None:
            if out:
                out += "\n"
            out += repr(result)

        return {
            "output": out.rstrip() if out else "",
            "error": "",
            "success": True,
        }
    except Exception as exc:
        out = stdout_capture.getvalue()
        err = _format_repl_error(exc)

        stderr_output = stderr_capture.getvalue()
        if stderr_output and stderr_output not in err:
            if err:
                err += "\n"
            err += stderr_output

        return {
            "output": out.rstrip() if out else "",
            "error": err.rstrip() if err else "",
            "success": False,
        }
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def worker_main(conn: Connection, cwd: str | None = None) -> None:
    """Worker event loop. Receives commands via conn, sends back results.

    Protocol:
        Command: {"cmd": "execute", "code": str}
        Command: {"cmd": "reset"}
        Command: {"cmd": "shutdown"}

        Response for execute: {"output": str, "error": str, "success": bool}
        Response for reset: {"status": "ok"}
    """
    if cwd:
        os.chdir(cwd)

    namespace: dict[str, Any] = {}

    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break

        cmd = msg.get("cmd")

        if cmd == "shutdown":
            conn.send({"status": "ok"})
            break
        elif cmd == "reset":
            namespace.clear()
            conn.send({"status": "ok"})
        elif cmd == "execute":
            code = msg["code"]
            result = _execute(code, namespace)
            conn.send(result)
        else:
            conn.send({"status": "error", "message": f"Unknown command: {cmd}"})

    conn.close()


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


# ---------------------------------------------------------------------------
# Slice resolution utility
# ---------------------------------------------------------------------------


def _resolve_slice(
    start: int | None,
    end: int | None,
    total: int,
) -> tuple[int, int]:
    """Resolve start/end to a Python slice range [s, e).

    Identical to Python's slice(start, end).indices(total),
    following standard Python slicing conventions:
      - 0-based indexing.
      - Half-open interval: [start, end) — start inclusive, end exclusive.
      - Negative values count from end: -1 = last item, -2 = second to last.
      - None defaults: start=None → 0, end=None → total.
      - Out-of-range values are clamped gracefully.

    Returns a tuple (s, e) suitable for list[s:e].
    """
    s, e, _ = slice(start, end).indices(total)
    return s, e


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def _log_execution(session_id: str, record: "ExecutionRecord") -> None:
    """Log execution input/output to stderr (MCP Logs panel)."""
    separator = "─" * 50
    print(separator, file=sys.stderr, flush=True)
    print(f"Session: {session_id} | {len(record.code.splitlines())} lines", file=sys.stderr, flush=True)
    print(">>> INPUT:", file=sys.stderr, flush=True)
    for line in record.code.strip().splitlines():
        print(f"  {line}", file=sys.stderr, flush=True)
    if record.success:
        if record.output:
            print("<<< OUTPUT:", file=sys.stderr, flush=True)
            for line in record.output.splitlines():
                print(f"  {line}", file=sys.stderr, flush=True)
        else:
            print("<<< (no output)", file=sys.stderr, flush=True)
    else:
        print("<<< ERROR:", file=sys.stderr, flush=True)
        for line in record.error.splitlines():
            print(f"  {line}", file=sys.stderr, flush=True)
    print(separator, file=sys.stderr, flush=True)


@dataclass
class Session:
    """A Python REPL session backed by a dedicated worker process."""

    session_id: str
    _process: multiprocessing.Process
    _conn: Connection
    history: list[ExecutionRecord] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    cwd: str | None = None

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def execute(self, code: str, timeout: int | None = None) -> ExecutionRecord:
        """Send code to the worker and get the result."""
        try:
            self._conn.send({"cmd": "execute", "code": code})
        except (OSError, BrokenPipeError):
            # Worker crashed before we could send - restart and retry
            self._restart_worker()
            self._conn.send({"cmd": "execute", "code": code})

        if timeout is not None:
            if not self._conn.poll(timeout):
                # Timeout - kill and restart worker
                self._kill_worker()
                self._start_worker()
                record = ExecutionRecord(
                    code=code,
                    output="",
                    error=f"ExecutionTimeout: code execution exceeded {timeout} seconds",
                    timestamp=time.time(),
                    success=False,
                )
                self.history.append(record)
                return record

        try:
            result = self._conn.recv()
        except (EOFError, OSError):
            # Worker crashed during execution - restart
            self._restart_worker()
            record = ExecutionRecord(
                code=code,
                output="",
                error="WorkerCrash: worker process died unexpectedly, session has been restarted",
                timestamp=time.time(),
                success=False,
            )
            self.history.append(record)
            return record

        record = ExecutionRecord(
            code=code,
            output=result.get("output", ""),
            error=result.get("error", ""),
            timestamp=time.time(),
            success=result.get("success", True),
        )
        self.history.append(record)

        # Log input/output to MCP Logs panel
        _log_execution(self.session_id, record)

        return record

    def reset(self) -> None:
        """Reset namespace and history."""
        self._conn.send({"cmd": "reset"})
        self._conn.recv()
        self.history.clear()

    def reset_run_context(self) -> None:
        """Kill the worker and start a fresh one. History is preserved."""
        self._kill_worker()
        self._start_worker()

    def shutdown(self) -> None:
        """Gracefully shut down the worker process."""
        try:
            self._conn.send({"cmd": "shutdown"})
            self._process.join(timeout=2)
        except (OSError, EOFError):
            pass
        if self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=1)
        self._conn.close()

    def _kill_worker(self) -> None:
        """Force kill the worker process."""
        try:
            self._process.kill()
            self._process.join(timeout=2)
        except Exception:
            pass
        self._conn.close()

    def _restart_worker(self) -> None:
        """Kill worker if alive and start a fresh one."""
        if self._process.is_alive():
            self._kill_worker()
        else:
            self._conn.close()
        self._start_worker()

    def _start_worker(self) -> None:
        """Start a new worker process."""
        parent_conn, child_conn = multiprocessing.Pipe()
        process = multiprocessing.Process(
            target=worker_main,
            args=(child_conn, self.cwd),
            daemon=True,
        )
        process.start()
        child_conn.close()
        self._process = process
        self._conn = parent_conn

    def format_history(self, start: int | None = None, end: int | None = None) -> str:
        """Format execution history like a Python interactive REPL."""
        total = len(self.history)
        s, e = _resolve_slice(start, end, total)
        records = self.history[s:e]
        parts: list[str] = []
        line_num = s + 1

        for record in records:
            lines = record.code.strip("\n").splitlines()
            prefix = f"[{line_num}]"
            for i, line in enumerate(lines):
                if i == 0:
                    parts.append(f"{prefix} {line}")
                else:
                    parts.append(f"{' ' * len(prefix)} {line}")

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
    """Manages multiple Python REPL sessions, each in its own process."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create_session(self, session_id: str | None = None, cwd: str | None = None) -> Session:
        """Create a new session with a dedicated worker process."""
        if session_id is None:
            session_id = str(uuid.uuid4())[:8]

        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session '{session_id}' already exists")

            parent_conn, child_conn = multiprocessing.Pipe()
            process = multiprocessing.Process(
                target=worker_main,
                args=(child_conn, cwd),
                daemon=True,
            )
            process.start()
            child_conn.close()

            session = Session(
                session_id=session_id,
                _process=process,
                _conn=parent_conn,
                cwd=cwd,
            )
            self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> Session:
        """Get an existing session by ID."""
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session '{session_id}' not found")
            return self._sessions[session_id]

    def delete_session(self, session_id: str) -> None:
        """Delete a session and shut down its worker."""
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session '{session_id}' not found")
            session = self._sessions.pop(session_id)
        session.shutdown()

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions with metadata."""
        with self._lock:
            return [
                {
                    "session_id": s.session_id,
                    "created_at": s.created_at,
                    "history_count": len(s.history),
                    "alive": s.is_alive(),
                }
                for s in self._sessions.values()
            ]


# ---------------------------------------------------------------------------
# Command execution utility
# ---------------------------------------------------------------------------


def cmd(args, input_values=None, on_input=None, encoding="gbk", chunk_size=1024, **kwargs):
    """Execute a command with streaming output and optional interactive input."""
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
    host=os.environ.get("MCP_HOST", "127.0.0.1").strip(),
    port=int(os.environ.get("MCP_PORT", "8000").strip()),
)

manager = SessionManager()


@mcp.tool()
def create_session(session_id: str | None = None, cwd: str | None = None) -> str:
    """Create a new Python REPL session.

    Each session has its own isolated namespace for variable storage
    and its own execution history.

    Args:
        session_id: Optional custom session ID. Auto-generated if not provided.
        cwd: Optional working directory for the session. Defaults to server's cwd.

    Returns:
        JSON with the created session info.
    """
    try:
        session = manager.create_session(session_id, cwd=cwd)
        return json.dumps({
            "status": "success",
            "session_id": session.session_id,
            "message": f"Session '{session.session_id}' created successfully",
        })
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
        session = manager.get_session(session_id)
        session.reset()
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
    recreates the runtime context: kills the worker process and starts a new one,
    providing a completely clean environment as if a new Python interpreter was started.

    The session keeps its history but all runtime state is discarded.

    Args:
        session_id: The ID of the session to reset.

    Returns:
        JSON with the operation result.
    """
    try:
        session = manager.get_session(session_id)
        session.reset_run_context()
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
    start: int | None = None,
    end: int | None = None,
    timeout: int | None = None,
) -> str:
    """Execute Python code in the specified session.

    The code runs within the session's namespace, so variables defined
    in previous executions are accessible. If the last statement is an
    expression, its value will be returned.

    There are two modes:
    1. Provide code directly (optionally sliced by start/end which refer
       to line numbers within the code).
    2. Omit code and use start/end to index into the session's history,
       concatenating the code from those blocks and executing the result.

    Indexing follows standard Python slicing conventions:
      - 0-based indexing.
      - Half-open interval [start, end): start inclusive, end exclusive.
      - Negative values count from end: -1 = last item.
      - None defaults: start=None → beginning, end=None → end.
      - Out-of-range values are clamped gracefully.

    Examples (assuming 5 history blocks [0..4]):
      start=0, end=2  → blocks 0 and 1
      start=-1        → last block only
      start=1, end=-1 → blocks 1, 2, 3 (excludes last)

    Args:
        session_id: The session to execute code in.
        code: The Python code to execute. If omitted, code is assembled from history.
        start: Start index (inclusive). See slicing rules above.
        end: End index (exclusive). See slicing rules above.
        timeout: Execution timeout in seconds. Default is None (no timeout).

    Returns:
        The execution result formatted as interactive Python REPL output.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    if code:
        if start is not None or end is not None:
            all_lines = code.splitlines()
            total = len(all_lines)
            s, e = _resolve_slice(start, end, total)
            if s >= e:
                return json.dumps({
                    "status": "error",
                    "message": f"Empty slice: start={start}, end={end} (code has {total} lines)",
                })
            code = "\n".join(all_lines[s:e])
    else:
        if start is None and end is None:
            return json.dumps({"status": "error", "message": "No code provided"})

        total = len(session.history)
        s, e = _resolve_slice(start, end, total)
        if s >= e:
            return json.dumps({
                "status": "error",
                "message": f"Empty slice: start={start}, end={end} (history has {total} blocks)",
            })

        code_parts: list[str] = []
        for record in session.history[s:e]:
            code_parts.append(record.code)

        if not code_parts:
            return json.dumps({"status": "error", "message": "No executable code in the specified range"})

        code = "\n".join(code_parts)

    record = session.execute(code, timeout=timeout)

    line_num = len(session.history)
    parts: list[str] = []
    lines = code.strip("\n").splitlines()
    prefix = f"[{line_num}]"
    for i, line in enumerate(lines):
        if i == 0:
            parts.append(f"{prefix} {line}")
        else:
            parts.append(f"{' ' * len(prefix)} {line}")

    if record.success:
        if record.output:
            parts.append(record.output)
    else:
        if record.error:
            parts.append(record.error)

    return "\n".join(parts)


@mcp.tool()
def run_file(
    session_id: str,
    path: str,
    timeout: int | None = None,
) -> str:
    """Execute a Python file in the specified session.

    Reads the file content and executes it within the session's namespace,
    equivalent to running `exec(open(path).read())` in the session.

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

    record = session.execute(code, timeout=timeout)

    line_num = len(session.history)
    filename = os.path.basename(abs_path)
    parts: list[str] = [f"[{line_num}] exec('{filename}')"]

    if record.success:
        if record.output:
            parts.append(record.output)
    else:
        if record.error:
            parts.append(record.error)

    return "\n".join(parts)


@mcp.tool()
def rerun_code(
    session_id: str,
    start: int | None = None,
    end: int | None = None,
    timeout: int | None = None,
) -> str:
    """Reset the session's run context and re-execute history code from scratch.

    This is equivalent to calling reset_run_context followed by sequentially
    re-executing each history block. Useful when you want a clean re-run of
    all (or a range of) previous code without accumulated side effects.

    The history is preserved before reset. After reset_run_context, each block
    in the specified range is executed in order. The new execution results
    replace the old history.

    Indexing follows standard Python slicing conventions:
      - 0-based, half-open interval [start, end).
      - Negative values count from end. None = default boundary.

    Args:
        session_id: The session to rerun.
        start: Start index (inclusive). Default: beginning.
        end: End index (exclusive). Default: end of history.
        timeout: Execution timeout in seconds per block. Default is None (no timeout).

    Returns:
        The execution results formatted as interactive Python REPL output.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    total = len(session.history)
    if total == 0:
        return json.dumps({"status": "error", "message": "No history to rerun"})

    s, e = _resolve_slice(start, end, total)
    if s >= e:
        return json.dumps({
            "status": "error",
            "message": f"Empty slice: start={start}, end={end} (history has {total} blocks)",
        })

    # Extract code from the target history range
    code_snippets = [record.code for record in session.history[s:e]]

    # Kill worker and start fresh (true reset)
    session.reset_run_context()
    session.history.clear()

    # Re-execute each record sequentially
    output_parts: list[str] = []
    for code in code_snippets:
        record = session.execute(code, timeout=timeout)
        line_num = len(session.history)

        lines = code.strip("\n").splitlines()
        prefix = f"[{line_num}]"
        for i, line in enumerate(lines):
            if i == 0:
                output_parts.append(f"{prefix} {line}")
            else:
                output_parts.append(f"{' ' * len(prefix)} {line}")

        if record.success:
            if record.output:
                output_parts.append(record.output)
        else:
            if record.error:
                output_parts.append(record.error)

        output_parts.append("")

    if output_parts and output_parts[-1] == "":
        output_parts.pop()

    return "\n".join(output_parts)


@mcp.tool()
def get_history(session_id: str, start: int | None = None, end: int | None = None) -> str:
    """Get the execution history of a session, formatted like a Python interactive REPL.

    Output looks like:
        [1] a = 1
        [2] a
        1
        [3] b
        Traceback (most recent call last):
          File "<stdin>", line 1, in <module>
        NameError: name 'b' is not defined

    Indexing follows standard Python slicing conventions:
      - 0-based, half-open interval [start, end).
      - Negative values count from end. None = default boundary.

    Examples (assuming 5 blocks):
      start=0, end=2  → first 2 blocks
      start=-2        → last 2 blocks
      (no args)       → all blocks

    Args:
        session_id: The session to get history from.
        start: Start index (inclusive). Default: beginning.
        end: End index (exclusive). Default: end of history.

    Returns:
        The execution history formatted as interactive Python REPL output.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    formatted = session.format_history(start, end)
    return formatted


@mcp.tool()
def delete_history(
    session_id: str,
    start: int,
    end: int | None = None,
) -> str:
    """Delete a range of history blocks from a session.

    Indexing follows standard Python slicing conventions:
      - 0-based, half-open interval [start, end).
      - Negative values count from end. None = default boundary.
      - If end is omitted, deletes the single block at start index.

    Examples:
      start=0, end=2  → delete first 2 blocks
      start=-1        → delete last block
      start=2         → delete block at index 2

    Args:
        session_id: The session to delete history from.
        start: Start index (inclusive).
        end: End index (exclusive). Defaults to start+1 (single block).

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

    if end is None:
        # Single block deletion: resolve start index then delete one
        s, _ = _resolve_slice(start, None, total)
        e = s + 1
        if s >= total:
            return json.dumps({
                "status": "error",
                "message": f"Index out of range: start={start} (history has {total} blocks)",
            })
    else:
        s, e = _resolve_slice(start, end, total)

    if s >= e:
        return json.dumps({
            "status": "error",
            "message": f"Empty slice: start={start}, end={end} (history has {total} blocks)",
        })

    deleted_count = e - s
    del session.history[s:e]

    return json.dumps({
        "status": "success",
        "message": f"Deleted {deleted_count} block(s) from history",
        "remaining": len(session.history),
    })


@mcp.tool()
def export_history(
    session_id: str,
    path: str,
    start: int | None = None,
    end: int | None = None,
) -> str:
    """Save execution history to a file, including code and output.

    Extracts the formatted execution history (code + output) from the
    specified range and writes to the given file path.

    Indexing follows standard Python slicing conventions:
      - 0-based, half-open interval [start, end).
      - Negative values count from end. None = default boundary.

    Args:
        session_id: The session to extract history from.
        path: File path to save the history (e.g. 'history.txt').
        start: Start index (inclusive). Default: beginning.
        end: End index (exclusive). Default: end of history.

    Returns:
        JSON with the operation result.
    """
    try:
        session = manager.get_session(session_id)
    except KeyError as e:
        return json.dumps({"status": "error", "message": str(e)})

    total = len(session.history)
    if total == 0:
        return json.dumps({"status": "error", "message": "No history to export"})

    s, e = _resolve_slice(start, end, total)
    if s >= e:
        return json.dumps({
            "status": "error",
            "message": f"Empty slice: start={start}, end={end} (history has {total} blocks)",
        })

    formatted = session.format_history(start, end)

    try:
        abs_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(formatted + "\n")
        return json.dumps({
            "status": "success",
            "message": f"History exported to '{abs_path}'",
            "path": abs_path,
            "blocks_exported": e - s,
        })
    except Exception as ex:
        return json.dumps({"status": "error", "message": f"Failed to write file: {str(ex)}"})


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
def save_script(
    session_id: str,
    path: str,
    start: int | None = None,
    end: int | None = None,
) -> str:
    """Save history code to a Python script file.

    Extracts code from the specified range, concatenates it,
    and writes to the given file path.

    Indexing follows standard Python slicing conventions:
      - 0-based, half-open interval [start, end).
      - Negative values count from end. None = default boundary.

    Args:
        session_id: The session to extract code from.
        path: File path to save the script (e.g. 'output.py').
        start: Start index (inclusive). Default: beginning.
        end: End index (exclusive). Default: end of history.

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

    s, e = _resolve_slice(start, end, total)
    if s >= e:
        return json.dumps({
            "status": "error",
            "message": f"Empty slice: start={start}, end={end} (history has {total} blocks)",
        })

    code_parts: list[str] = []
    for record in session.history[s:e]:
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
            "blocks_saved": len(code_parts),
        })
    except Exception as ex:
        return json.dumps({"status": "error", "message": f"Failed to write file: {str(ex)}"})


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
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip()
    if transport not in ("stdio", "sse", "streamable-http"):
        raise ValueError(
            f"Invalid MCP_TRANSPORT='{transport}'. "
            f"Must be one of: stdio, sse, streamable-http"
        )
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
