"""Session worker process - runs in a separate process for true isolation."""

from __future__ import annotations

import ast
import io
import sys
import traceback
from multiprocessing.connection import Connection
from typing import Any


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


def worker_main(conn: Connection) -> None:
    """Worker event loop. Receives commands via conn, sends back results.

    Protocol:
        Command: {"cmd": "execute", "code": str}
        Command: {"cmd": "reset"}
        Command: {"cmd": "shutdown"}

        Response for execute: {"output": str, "error": str, "success": bool}
        Response for reset: {"status": "ok"}
    """
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
