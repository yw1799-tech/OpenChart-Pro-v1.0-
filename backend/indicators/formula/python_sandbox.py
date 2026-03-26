"""
Python 高级模式沙箱
使用 RestrictedPython 在安全沙箱中执行用户 Python 代码。
用户代码必须定义 calculate(open, high, low, close, volume) 函数。
"""

import threading
import numpy as np
from typing import Any

try:
    from RestrictedPython import compile_restricted, safe_globals
    from RestrictedPython.Eval import default_guarded_getiter, default_guarded_getitem
    from RestrictedPython.Guards import (
        guarded_unpack_sequence,
        safer_getattr,
    )
    HAS_RESTRICTED_PYTHON = True
except ImportError:
    HAS_RESTRICTED_PYTHON = False


class PythonSandboxError(Exception):
    """Python 沙箱执行错误"""
    pass


class PythonTimeoutError(Exception):
    """Python 沙箱执行超时"""
    pass


# 禁止的模块
_FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "socket", "http",
    "urllib", "requests", "ctypes", "signal", "multiprocessing",
    "threading", "asyncio", "importlib", "pickle", "shelve",
    "sqlite3", "pathlib", "glob", "tempfile", "io",
}

# 禁止的代码模式
_FORBIDDEN_PATTERNS = [
    "__import__",
    "exec(",
    "eval(",
    "compile(",
    "globals(",
    "locals(",
    "breakpoint(",
    "__builtins__",
    "__class__",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "os.system",
    "os.popen",
    "subprocess",
    "open(",  # 文件操作的 open，不是 OHLCV 的 open
]


def _check_code_safety(code: str) -> list[str]:
    """检查代码安全性，返回错误列表"""
    errors = []

    # 检查 import 语句
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            # 提取模块名
            if stripped.startswith("import "):
                module = stripped.split()[1].split(".")[0]
            else:
                module = stripped.split()[1].split(".")[0]

            if module in _FORBIDDEN_MODULES:
                errors.append(f"第{i}行: 禁止导入模块 '{module}'")

    # 检查危险模式
    for pattern in _FORBIDDEN_PATTERNS:
        # 特殊处理 open(：需要区分函数调用和变量名
        if pattern == "open(":
            # 查找非 OHLCV 上下文的 open() 调用
            import re
            # 匹配独立的 open( 但排除 ohlcv 数据引用上下文
            matches = list(re.finditer(r'\bopen\s*\(', code))
            for m in matches:
                # 检查是否在 def calculate 的参数列表中
                before = code[:m.start()]
                if "def " in before.split("\n")[-1]:
                    continue  # 函数定义中的参数
                errors.append(f"禁止使用文件操作函数 open()")
                break
        elif pattern in code:
            errors.append(f"代码包含禁止的操作: {pattern}")

    return errors


def _safe_import(name, *args, **kwargs):
    """受限的 import 函数，只允许白名单模块"""
    allowed = {"numpy", "math", "statistics", "pandas"}
    if name in allowed:
        import importlib
        return importlib.import_module(name)
    raise ImportError(f"禁止导入模块: {name}. 仅允许: {', '.join(sorted(allowed))}")


def _run_with_timeout(func, timeout: float):
    """在超时限制内运行函数"""
    result = [None]
    error = [None]

    def target():
        try:
            result[0] = func()
        except Exception as e:
            error[0] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        raise PythonTimeoutError(f"代码执行超时 ({timeout}秒)")

    if error[0] is not None:
        raise error[0]

    return result[0]


def _build_safe_globals() -> dict:
    """构建安全的全局变量字典"""
    import math
    import statistics

    g = {"__builtins__": {}}

    # 安全的内置函数
    safe_builtins = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "filter": filter,
        "float": float,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "pow": pow,
        "print": print,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "type": type,
        "zip": zip,
        "True": True,
        "False": False,
        "None": None,
        "__import__": _safe_import,
    }
    g["__builtins__"] = safe_builtins

    # 白名单库
    g["np"] = np
    g["numpy"] = np
    g["math"] = math
    g["statistics"] = statistics

    # 尝试导入 pandas
    try:
        import pandas as pd
        g["pd"] = pd
        g["pandas"] = pd
    except ImportError:
        pass

    # RestrictedPython 守卫函数
    if HAS_RESTRICTED_PYTHON:
        g["_getiter_"] = default_guarded_getiter
        g["_getitem_"] = default_guarded_getitem
        g["_unpack_sequence_"] = guarded_unpack_sequence
        g["_getattr_"] = safer_getattr
        g["_write_"] = lambda x: x
        g["_inplacevar_"] = lambda op, x, y: op(x, y)

    return g


def execute_python(
    code: str,
    ohlcv_data: dict[str, list | np.ndarray],
    timeout: float = 10.0,
) -> dict:
    """
    在安全沙箱中执行 Python 代码。

    用户代码必须定义 calculate(open, high, low, close, volume) 函数，
    该函数返回包含绘图指令的字典。

    Args:
        code: Python 源代码
        ohlcv_data: OHLCV 数据字典
        timeout: 执行超时时间（秒），默认10秒

    Returns:
        {
            "plots": [...],
            "shapes": [...],
            "alerts": [...],
            "inputs": [],
            "meta": {"mode": "python", "name": "Python Script"},
        }

    Raises:
        PythonSandboxError: 执行失败
        PythonTimeoutError: 执行超时
    """
    # 步骤1：代码安全检查
    safety_errors = _check_code_safety(code)
    if safety_errors:
        raise PythonSandboxError("代码安全检查失败:\n" + "\n".join(safety_errors))

    # 步骤2：检查是否定义了 calculate 函数
    if "def calculate" not in code:
        raise PythonSandboxError(
            "代码必须定义 calculate(open, high, low, close, volume) 函数"
        )

    # 步骤3：转换 OHLCV 数据为 numpy 数组
    ohlcv = {}
    for key in ("open", "high", "low", "close", "volume"):
        data = ohlcv_data.get(key, [])
        if isinstance(data, np.ndarray):
            ohlcv[key] = data.astype(np.float64)
        else:
            ohlcv[key] = np.array(data, dtype=np.float64)

    # 步骤4：编译代码
    if HAS_RESTRICTED_PYTHON:
        # 使用 RestrictedPython 编译
        result = compile_restricted(code, "<python_sandbox>", "exec")
        if result.errors:
            raise PythonSandboxError(
                "RestrictedPython 编译错误:\n" + "\n".join(result.errors)
            )
        compiled = result.code
    else:
        # 降级：使用标准编译（仍有运行时守卫）
        try:
            compiled = compile(code, "<python_sandbox>", "exec")
        except SyntaxError as e:
            raise PythonSandboxError(f"语法错误 (行 {e.lineno}): {e.msg}") from e

    # 步骤5：构建安全的全局变量
    safe_g = _build_safe_globals()

    # 步骤6：执行代码定义 calculate 函数
    def run():
        exec(compiled, safe_g)

        # 检查 calculate 是否被定义
        if "calculate" not in safe_g:
            raise PythonSandboxError("未找到 calculate 函数定义")

        calculate_func = safe_g["calculate"]

        # 调用 calculate 函数
        calc_result = calculate_func(
            ohlcv["open"],
            ohlcv["high"],
            ohlcv["low"],
            ohlcv["close"],
            ohlcv["volume"],
        )

        return calc_result

    try:
        calc_result = _run_with_timeout(run, timeout)
    except PythonTimeoutError:
        raise
    except PythonSandboxError:
        raise
    except Exception as e:
        raise PythonSandboxError(f"执行失败: {type(e).__name__}: {e}") from e

    # 步骤7：规范化返回结果
    return _normalize_result(calc_result)


def _normalize_result(calc_result: Any) -> dict:
    """将 calculate() 返回值规范化为标准输出格式"""
    output = {
        "plots": [],
        "shapes": [],
        "alerts": [],
        "inputs": [],
        "meta": {
            "mode": "python",
            "name": "Python Script",
        },
    }

    if calc_result is None:
        return output

    if isinstance(calc_result, dict):
        # 用户直接返回标准格式
        if "plots" in calc_result:
            plots = calc_result["plots"]
            if isinstance(plots, list):
                output["plots"] = _convert_plot_list(plots)

        if "shapes" in calc_result:
            shapes = calc_result["shapes"]
            if isinstance(shapes, list):
                output["shapes"] = shapes

        if "alerts" in calc_result:
            alerts = calc_result["alerts"]
            if isinstance(alerts, list):
                output["alerts"] = alerts

        if "meta" in calc_result and isinstance(calc_result["meta"], dict):
            output["meta"].update(calc_result["meta"])

    elif isinstance(calc_result, (list, tuple)):
        # 用户返回数组列表，每个当作一条 plot
        for i, item in enumerate(calc_result):
            if isinstance(item, dict):
                output["plots"].append(item)
            elif isinstance(item, np.ndarray):
                output["plots"].append({
                    "type": "plot",
                    "title": f"Line {i + 1}",
                    "data": item.tolist(),
                })
            elif isinstance(item, list):
                output["plots"].append({
                    "type": "plot",
                    "title": f"Line {i + 1}",
                    "data": item,
                })

    elif isinstance(calc_result, np.ndarray):
        # 单个数组
        output["plots"].append({
            "type": "plot",
            "title": "Result",
            "data": calc_result.tolist(),
        })

    return output


def _convert_plot_list(plots: list) -> list:
    """转换 plot 列表中的 numpy 数组为 list"""
    converted = []
    for p in plots:
        if isinstance(p, dict):
            new_p = {}
            for k, v in p.items():
                if isinstance(v, np.ndarray):
                    new_p[k] = v.tolist()
                else:
                    new_p[k] = v
            converted.append(new_p)
        elif isinstance(p, np.ndarray):
            converted.append({
                "type": "plot",
                "data": p.tolist(),
            })
        else:
            converted.append(p)
    return converted


def validate_python_code(code: str) -> dict:
    """
    验证 Python 代码（不执行）。

    Returns:
        {"valid": bool, "errors": [...]}
    """
    errors = []

    # 安全检查
    safety_errors = _check_code_safety(code)
    errors.extend(safety_errors)

    # 检查 calculate 函数
    if "def calculate" not in code:
        errors.append("代码必须定义 calculate(open, high, low, close, volume) 函数")

    # 语法检查
    if not errors:
        if HAS_RESTRICTED_PYTHON:
            result = compile_restricted(code, "<validate>", "exec")
            if result.errors:
                errors.extend(result.errors)
        else:
            try:
                compile(code, "<validate>", "exec")
            except SyntaxError as e:
                errors.append(f"语法错误 (行 {e.lineno}): {e.msg}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
    }
