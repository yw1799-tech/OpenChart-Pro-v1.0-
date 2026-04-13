"""
Pine Script 兼容解释器（逐bar执行）
基于 parser.py 的 AST，对每根K线执行脚本，维护变量状态和技术指标历史。
支持 ta.*/math.*/array.*/strategy.* 内置函数、var/varip 持久变量、
if-else/for/while 控制流等。

输入：OHLCV numpy 数组
输出：plots/shapes/alerts/orders 列表
"""

from __future__ import annotations
import math
import threading
import numpy as np
from typing import Any

from .parser import (
    parse_openscript,
    OpenScriptError,
    ParseResult,
    Program,
    ASTNode,
    NumberLiteral,
    StringLiteral,
    BoolLiteral,
    NALiteral,
    Identifier,
    DotAccess,
    IndexAccess,
    BinaryOp,
    UnaryOp,
    TernaryOp,
    FunctionCall,
    Assignment,
    Reassignment,
    CompoundAssignment,
    IfBlock,
    ForBlock,
    WhileBlock,
    BreakStmt,
    ContinueStmt,
    ExprStatement,
    TupleDestructure,
    FunctionDef,
    _get_func_name,
)


class ExecutionError(Exception):
    """执行错误"""

    pass


class _TimeoutError(Exception):
    """执行超时"""

    pass


class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


# ============================================================
# Pine Script Array 包装类
# ============================================================


class PineArray:
    """Pine Script array 类型的 Python 实现"""

    def __init__(self, size: int = 0, init_val: float = float("nan")):
        if size > 0:
            self.data = [init_val] * size
        else:
            self.data = []

    def get(self, index: int):
        if 0 <= index < len(self.data):
            return self.data[index]
        return float("nan")

    def set(self, index: int, value):
        if 0 <= index < len(self.data):
            self.data[index] = value

    def push(self, value):
        self.data.append(value)

    def pop(self):
        if self.data:
            return self.data.pop()
        return float("nan")

    def size(self):
        return len(self.data)

    def sum(self):
        return sum(v for v in self.data if v is not None and not _is_nan(v))

    def avg(self):
        valid = [v for v in self.data if v is not None and not _is_nan(v)]
        return sum(valid) / len(valid) if valid else float("nan")

    def min(self):
        valid = [v for v in self.data if v is not None and not _is_nan(v)]
        return min(valid) if valid else float("nan")

    def max(self):
        valid = [v for v in self.data if v is not None and not _is_nan(v)]
        return max(valid) if valid else float("nan")

    def includes(self, value):
        return value in self.data

    def clear(self):
        self.data.clear()

    def copy(self):
        new_arr = PineArray()
        new_arr.data = list(self.data)
        return new_arr

    def remove(self, index: int):
        if 0 <= index < len(self.data):
            return self.data.pop(index)
        return float("nan")

    def insert(self, index: int, value):
        self.data.insert(index, value)

    def unshift(self, value):
        self.data.insert(0, value)

    def shift(self):
        if self.data:
            return self.data.pop(0)
        return float("nan")

    def slice(self, start: int, end: int = None):
        new_arr = PineArray()
        if end is None:
            new_arr.data = list(self.data[start:])
        else:
            new_arr.data = list(self.data[start:end])
        return new_arr


def _is_nan(v) -> bool:
    if v is None:
        return True
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


# ============================================================
# 技术指标状态管理器
# ============================================================


class TAState:
    """
    管理逐bar技术指标计算所需的历史数据。
    每个指标实例由 (函数名, 参数hash) 唯一标识。
    """

    def __init__(self):
        self.history: dict[str, list] = {}  # key -> 历史值列表
        self.state: dict[str, dict] = {}  # key -> 中间状态

    def get_history(self, key: str) -> list:
        if key not in self.history:
            self.history[key] = []
        return self.history[key]

    def get_state(self, key: str) -> dict:
        if key not in self.state:
            self.state[key] = {}
        return self.state[key]

    def reset(self):
        self.history.clear()
        self.state.clear()


# ============================================================
# 解释器上下文
# ============================================================


class InterpreterContext:
    """解释器执行上下文，管理变量作用域和bar数据"""

    def __init__(self, ohlcv: dict[str, np.ndarray], user_params: dict | None = None):
        self.ohlcv = ohlcv
        self.user_params = user_params or {}
        self.num_bars = len(ohlcv.get("close", []))

        # 当前bar索引
        self.bar_index = 0

        # 变量存储
        self.variables: dict[str, Any] = {}
        self.var_declared: set[str] = set()  # 已用 = 声明的变量
        self.var_persistent: set[str] = set()  # var 声明的持久变量
        self.varip_persistent: set[str] = set()  # varip 声明

        # 输出收集
        self.plots: list[dict] = []
        self.shapes: list[dict] = []
        self.alerts: list[dict] = []
        self.orders: list[dict] = []
        self._drawings: list[dict] = []  # line.new/label.new/box.new 绘图对象

        # 每个 plot/shape/alert 的 per-bar 数据
        self._plot_series: dict[int, list] = {}  # plot_id -> [values per bar]
        self._shape_series: dict[int, list] = {}
        self._alert_series: dict[int, list] = {}
        self._plot_configs: dict[int, dict] = {}
        self._shape_configs: dict[int, dict] = {}
        self._alert_configs: dict[int, dict] = {}
        self._plot_counter = 0
        self._shape_counter = 0
        self._alert_counter = 0

        # hline 收集
        self._hlines: list[dict] = []

        # 技术指标状态
        self.ta_state = TAState()

        # 指标调用计数器（每个bar重置，用于生成唯一key）
        self._ta_call_counter = 0

        # 策略状态
        self.strategy_mode = False
        self.strategy_name = ""
        self.initial_capital = 100000

        # 超时检测
        self._start_time = 0
        self._timeout = 10.0
        self._op_count = 0

    def check_timeout(self):
        """每隔一定操作次数检查超时"""
        self._op_count += 1
        if self._op_count % 5000 == 0:
            import time

            if time.time() - self._start_time > self._timeout:
                raise _TimeoutError(f"代码执行超时 ({self._timeout}秒)")


# ============================================================
# Pine Script 解释器
# ============================================================


class PineInterpreter:
    """
    Pine Script AST 解释器。
    对每根K线执行一次完整脚本，维护跨bar状态。
    """

    def __init__(self, ctx: InterpreterContext):
        self.ctx = ctx

    @staticmethod
    def _safe_int(v, default=0):
        """安全转换为int，None/na返回default"""
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(v, default=0.0):
        """安全转换为float，None/na返回default"""
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def run(self, program: Program):
        """执行完整程序（逐bar）"""
        import time

        self.ctx._start_time = time.time()

        _errors = []
        for bar_idx in range(self.ctx.num_bars):
            self.ctx.bar_index = bar_idx
            self.ctx._ta_call_counter = 0  # 每个bar重置
            try:
                self._execute_block(program.statements)
            except ExecutionError:
                raise  # 超时等严重错误继续抛出
            except Exception as e:
                if len(_errors) < 3:  # 只记录前3个错误
                    _errors.append(f"bar {bar_idx}: {type(e).__name__}: {e}")
                # 继续执行下一个bar
            self.ctx.check_timeout()

        self._finalize_outputs()

    def _execute_block(self, stmts: list[ASTNode]):
        for stmt in stmts:
            self._execute_stmt(stmt)

    def _execute_stmt(self, node: ASTNode):
        self.ctx.check_timeout()

        if isinstance(node, Assignment):
            self._exec_assignment(node)
        elif isinstance(node, Reassignment):
            self._exec_reassignment(node)
        elif isinstance(node, CompoundAssignment):
            self._exec_compound_assignment(node)
        elif isinstance(node, ExprStatement):
            self._eval(node.expr)
        elif isinstance(node, IfBlock):
            self._exec_if(node)
        elif isinstance(node, ForBlock):
            self._exec_for(node)
        elif isinstance(node, WhileBlock):
            self._exec_while(node)
        elif isinstance(node, TupleDestructure):
            self._exec_tuple_destructure(node)
        elif isinstance(node, FunctionDef):
            self._exec_function_def(node)
        elif isinstance(node, BreakStmt):
            raise BreakSignal()
        elif isinstance(node, ContinueStmt):
            raise ContinueSignal()

    def _exec_assignment(self, node: Assignment):
        name = node.name

        # var 声明：只在第一个bar初始化
        if node.is_var or node.is_varip:
            if self.ctx.bar_index == 0:
                value = self._eval(node.value)
                self.ctx.variables[name] = value
                self.ctx.var_declared.add(name)
                if node.is_var:
                    self.ctx.var_persistent.add(name)
                if node.is_varip:
                    self.ctx.varip_persistent.add(name)
            else:
                # 后续bar仍然需要评估表达式以保持 _ta_call_counter 一致
                # 但丢弃结果，保持持久变量值不变
                self._eval(node.value)
            # 后续bar跳过初始化（保持持久值）
            return

        # 普通声明：每个bar都执行
        value = self._eval(node.value)
        self.ctx.variables[name] = value
        self.ctx.var_declared.add(name)

    def _exec_reassignment(self, node: Reassignment):
        value = self._eval(node.value)
        self.ctx.variables[node.name] = value

    def _exec_compound_assignment(self, node: CompoundAssignment):
        old_val = self.ctx.variables.get(node.name, 0)
        rhs = self._eval(node.value)
        op_map = {
            "+": lambda a, b: a + b,
            "-": lambda a, b: a - b,
            "*": lambda a, b: a * b,
            "/": lambda a, b: a / b if b != 0 else float("nan"),
        }
        self.ctx.variables[node.name] = op_map[node.op](old_val, rhs)

    def _exec_if(self, node: IfBlock):
        cond = self._eval(node.condition)
        if self._is_truthy(cond):
            self._execute_block(node.body)
            return

        for elif_cond, elif_body in node.elif_blocks:
            cond = self._eval(elif_cond)
            if self._is_truthy(cond):
                self._execute_block(elif_body)
                return

        if node.else_body:
            self._execute_block(node.else_body)

    def _exec_for(self, node: ForBlock):
        start_val = self._safe_int(self._eval(node.start))
        end_val = self._safe_int(self._eval(node.end))
        step_val = self._safe_int(self._eval(node.step)) if node.step else 1
        if step_val == 0:
            step_val = 1

        # 安全限制：最多10000次迭代
        max_iters = 10000
        count = 0

        if step_val > 0:
            i = start_val
            while i <= end_val and count < max_iters:
                self.ctx.variables[node.var_name] = i
                try:
                    self._execute_block(node.body)
                except BreakSignal:
                    break
                except ContinueSignal:
                    pass
                i += step_val
                count += 1
        else:
            i = start_val
            while i >= end_val and count < max_iters:
                self.ctx.variables[node.var_name] = i
                try:
                    self._execute_block(node.body)
                except BreakSignal:
                    break
                except ContinueSignal:
                    pass
                i += step_val
                count += 1

    def _exec_while(self, node: WhileBlock):
        max_iters = 10000
        count = 0
        while count < max_iters:
            cond = self._eval(node.condition)
            if not self._is_truthy(cond):
                break
            try:
                self._execute_block(node.body)
            except BreakSignal:
                break
            except ContinueSignal:
                pass
            count += 1

    def _exec_tuple_destructure(self, node: TupleDestructure):
        value = self._eval(node.value)
        if isinstance(value, (list, tuple)):
            for i, name in enumerate(node.names):
                self.ctx.variables[name] = value[i] if i < len(value) else None
        else:
            # 单值赋给第一个
            if node.names:
                self.ctx.variables[node.names[0]] = value

    def _exec_function_def(self, node: FunctionDef):
        """存储用户定义函数到环境中（只在第一个bar存储即可，但每次都执行无害）"""
        self.ctx.variables[node.name] = _UserFunction(node.name, node.params, node.body)

    # --- 表达式求值 ---

    def _eval(self, node: ASTNode) -> Any:
        self.ctx.check_timeout()

        if isinstance(node, NumberLiteral):
            return node.value
        if isinstance(node, StringLiteral):
            return node.value
        if isinstance(node, BoolLiteral):
            return node.value
        if isinstance(node, NALiteral):
            return None
        if isinstance(node, Identifier):
            return self._resolve_identifier(node.name)
        if isinstance(node, DotAccess):
            return self._eval_dot_access(node)
        if isinstance(node, IndexAccess):
            return self._eval_index_access(node)
        if isinstance(node, BinaryOp):
            return self._eval_binary(node)
        if isinstance(node, UnaryOp):
            return self._eval_unary(node)
        if isinstance(node, TernaryOp):
            return self._eval_ternary(node)
        if isinstance(node, FunctionCall):
            return self._eval_call(node)
        if isinstance(node, IfBlock):
            return self._eval_if_expr(node)

        # 未知节点类型，返回None而不是报错
        return None

    def _resolve_identifier(self, name: str) -> Any:
        """解析标识符"""
        # 用户变量
        if name in self.ctx.variables:
            return self.ctx.variables[name]

        # 用户参数
        if name in self.ctx.user_params:
            return self.ctx.user_params[name]

        # 内置bar数据
        bar_idx = self.ctx.bar_index
        ohlcv = self.ctx.ohlcv
        if name == "open" and "open" in ohlcv:
            return float(ohlcv["open"][bar_idx])
        if name == "high" and "high" in ohlcv:
            return float(ohlcv["high"][bar_idx])
        if name == "low" and "low" in ohlcv:
            return float(ohlcv["low"][bar_idx])
        if name == "close" and "close" in ohlcv:
            return float(ohlcv["close"][bar_idx])
        if name == "volume" and "volume" in ohlcv:
            return float(ohlcv["volume"][bar_idx])
        if name == "bar_index":
            return bar_idx
        if name == "time":
            return bar_idx  # 简化：用索引代替时间戳
        if name == "timenow":
            return self.ctx.num_bars - 1

        # 内置常量
        if name == "na":
            return None
        if name == "true":
            return True
        if name == "false":
            return False

        # 命名空间前缀（作为标识符返回，点访问时再处理）
        if name in (
            "ta",
            "math",
            "array",
            "str",
            "color",
            "strategy",
            "input",
            "plot",
            "hline",
            "fill",
            "bgcolor",
            "plotshape",
            "alertcondition",
            "indicator",
            "nz",
            "na",
            "fixnan",
            "sma",
            "ema",
            "rsi",
            "macd",
            "crossover",
            "crossunder",
            "highest",
            "lowest",
            "stdev",
            "wma",
            "rising",
            "falling",
            "valuewhen",
            "barssince",
            "request",
            "syminfo",
            "timeframe",
            "barstate",
            "line",
            "label",
            "box",
            "table",
        ):
            return _BuiltinNamespace(name)

        return None

    def _eval_dot_access(self, node: DotAccess) -> Any:
        obj = self._eval(node.obj)

        # 处理命名空间
        if isinstance(obj, _BuiltinNamespace):
            full_name = f"{obj.name}.{node.attr}"

            # TradingView 专属属性，直接返回值
            if full_name == "syminfo.tickerid":
                return self.ctx.ohlcv.get("_symbol", "UNKNOWN")
            if full_name == "syminfo.ticker":
                return self.ctx.ohlcv.get("_symbol", "UNKNOWN")
            if full_name == "timeframe.period":
                return self.ctx.ohlcv.get("_timeframe", "D")
            if full_name == "timeframe.multiplier":
                return 1
            if full_name == "timeframe.isintraday":
                return False
            if full_name == "timeframe.isdaily":
                return True
            if full_name == "barstate.islast":
                return self.ctx.bar_index == self.ctx.num_bars - 1
            if full_name == "barstate.isfirst":
                return self.ctx.bar_index == 0
            if full_name == "barstate.isconfirmed":
                return True
            if full_name == "barstate.isnew":
                return True
            if full_name == "barstate.isrealtime":
                return False
            if full_name == "barstate.ishistory":
                return True
            if full_name == "strategy.long":
                return "long"
            if full_name == "strategy.short":
                return "short"

            # 颜色常量: color.red → "#F44336"
            if full_name in self._COLOR_MAP:
                return self._COLOR_MAP[full_name]

            # size 常量
            if full_name.startswith("size."):
                return full_name  # size.small, size.normal 等直接返回字符串
            # text.align 常量
            if full_name.startswith("text."):
                return full_name

            return _BuiltinNamespace(full_name)

        # 处理 PineArray 方法
        if isinstance(obj, PineArray):
            method_name = node.attr
            if hasattr(obj, method_name):
                return _BoundMethod(obj, method_name)
            return None

        # 处理 list/tuple 属性
        if isinstance(obj, (list, tuple)):
            if node.attr == "length":
                return len(obj)

        return None

    def _eval_index_access(self, node: IndexAccess) -> Any:
        index = self._eval(node.index)

        # 历史引用 source[n] — Pine Script 中 close[1] 表示前1根bar之前的值
        # 当 obj 是标识符时，先检查是否是 OHLCV 数据源或用户变量的历史引用
        if isinstance(node.obj, Identifier):
            name = node.obj.name
            n = self._safe_int(index) if index is not None else 0
            bar_idx = self.ctx.bar_index

            # OHLCV 数据源历史引用
            if name in self.ctx.ohlcv:
                target_idx = bar_idx - n
                if 0 <= target_idx < self.ctx.num_bars:
                    return float(self.ctx.ohlcv[name][target_idx])
                return None

            # bar_index 历史引用
            if name == "bar_index":
                return max(0, bar_idx - n)

            # 用户变量历史引用：通过 ta_state 存储历史
            if name in self.ctx.variables:
                hist_key = f"__var_hist_{name}"
                history = self.ctx.ta_state.get_history(hist_key)
                # 确保历史记录已更新到当前bar（由 _track_var_history 处理）
                if n == 0:
                    return self.ctx.variables[name]
                if len(history) > n:
                    return history[-(n + 1)]
                return None

        obj = self._eval(node.obj)

        if obj is None:
            return None

        if isinstance(obj, PineArray):
            return obj.get(self._safe_int(index))
        if isinstance(obj, (list, tuple)):
            idx = self._safe_int(index)
            if 0 <= idx < len(obj):
                return obj[idx]
            return None
        if isinstance(obj, np.ndarray):
            idx = self._safe_int(index)
            if 0 <= idx < len(obj):
                return float(obj[idx])
            return None

        # 对于数值类型的历史引用（如计算结果[n]），暂返回当前值
        if isinstance(obj, (int, float)) and self._safe_int(index) == 0:
            return obj

        return None

    def _eval_binary(self, node: BinaryOp) -> Any:
        left = self._eval(node.left)
        right = self._eval(node.right)

        # 短路逻辑
        if node.op == "and":
            return self._is_truthy(left) and self._is_truthy(right)
        if node.op == "or":
            return self._is_truthy(left) or self._is_truthy(right)

        # None 处理
        if left is None or right is None:
            if node.op in ("==", "!=", ">=", "<=", ">", "<"):
                if node.op == "==":
                    return left is None and right is None
                elif node.op == "!=":
                    return not (left is None and right is None)
                else:
                    # 比较运算: None当0处理
                    left = 0 if left is None else left
                    right = 0 if right is None else right
            else:
                # 算术运算: None当0处理
                left = 0 if left is None else left
                right = 0 if right is None else right

        try:
            if node.op == "+":
                if isinstance(left, str) or isinstance(right, str):
                    return str(left) + str(right)
                return left + right
            if node.op == "-":
                return left - right
            if node.op == "*":
                return left * right
            if node.op == "/":
                if right == 0:
                    return None
                return left / right
            if node.op == "%":
                if right == 0:
                    return None
                return left % right
            if node.op == "==":
                return left == right
            if node.op == "!=":
                return left != right
            if node.op == "<":
                return left < right
            if node.op == ">":
                return left > right
            if node.op == "<=":
                return left <= right
            if node.op == ">=":
                return left >= right
        except TypeError:
            return None

        return None

    def _eval_unary(self, node: UnaryOp) -> Any:
        operand = self._eval(node.operand)
        if node.op == "-":
            if operand is None:
                return None
            return -operand
        if node.op == "not":
            return not self._is_truthy(operand)
        return operand

    def _eval_ternary(self, node: TernaryOp) -> Any:
        cond = self._eval(node.condition)
        if self._is_truthy(cond):
            return self._eval(node.true_expr)
        return self._eval(node.false_expr)

    def _eval_if_expr(self, node: IfBlock) -> Any:
        """if 作为表达式使用时返回最后一个表达式的值"""
        cond = self._eval(node.condition)
        if self._is_truthy(cond):
            return self._eval_block_as_expr(node.body)
        for elif_cond, elif_body in node.elif_blocks:
            c = self._eval(elif_cond)
            if self._is_truthy(c):
                return self._eval_block_as_expr(elif_body)
        if node.else_body:
            return self._eval_block_as_expr(node.else_body)
        return None

    def _eval_block_as_expr(self, stmts: list[ASTNode]) -> Any:
        result = None
        for stmt in stmts:
            if isinstance(stmt, ExprStatement):
                result = self._eval(stmt.expr)
            else:
                self._execute_stmt(stmt)
        return result

    # --- 函数调用 ---

    def _eval_call(self, node: FunctionCall) -> Any:
        func_name = _get_func_name(node.func)
        args = [self._eval(a) for a in node.args]
        kwargs = {k: self._eval(v) for k, v in node.kwargs.items()}

        # 特殊处理：__array_literal__ (解析器生成的数组字面量)
        if func_name == "__array_literal__":
            return list(args)

        # 处理 _BuiltinNamespace 的函数调用
        if isinstance(node.func, Identifier):
            ns = self._resolve_identifier(node.func.name)
            if isinstance(ns, _BuiltinNamespace):
                return self._call_builtin(ns.name, args, kwargs)
            # 用户定义函数
            if node.func.name in self.ctx.variables:
                val = self.ctx.variables[node.func.name]
                if isinstance(val, _UserFunction):
                    return self._call_user_function(val, args)
                if callable(val):
                    return val(*args, **kwargs)

        if isinstance(node.func, DotAccess):
            resolved = self._eval(node.func)
            if isinstance(resolved, _BuiltinNamespace):
                return self._call_builtin(resolved.name, args, kwargs)
            if isinstance(resolved, _BoundMethod):
                return getattr(resolved.obj, resolved.method)(*args)

        # 直接名称匹配内置函数
        if func_name:
            return self._call_builtin(func_name, args, kwargs)

        # 无法识别的函数调用，返回None而不是报错（Pine Script中很多函数我们不支持）
        return None

    def _call_user_function(self, func: "_UserFunction", args: list) -> Any:
        """调用用户定义函数"""
        # 保存当前变量（简单作用域隔离）
        saved_vars = {}
        for param_name in func.params:
            if param_name in self.ctx.variables:
                saved_vars[param_name] = self.ctx.variables[param_name]

        # 绑定参数
        for i, param_name in enumerate(func.params):
            self.ctx.variables[param_name] = args[i] if i < len(args) else None

        # 执行函数体，最后一个表达式的值作为返回值
        result = None
        for stmt in func.body:
            if isinstance(stmt, ExprStatement):
                result = self._eval(stmt.expr)
            else:
                self._execute_stmt(stmt)
                # 如果是赋值语句，结果是赋值的值（Pine Script 中函数最后一行）
                if isinstance(stmt, Assignment):
                    result = self.ctx.variables.get(stmt.name)

        # 恢复被覆盖的变量
        for param_name in func.params:
            if param_name in saved_vars:
                self.ctx.variables[param_name] = saved_vars[param_name]
            elif param_name not in self.ctx.var_declared:
                # 局部参数变量，函数结束后移除
                self.ctx.variables.pop(param_name, None)

        return result

    def _call_builtin(self, name: str, args: list, kwargs: dict) -> Any:
        """调用内置函数"""
        bar_idx = self.ctx.bar_index
        ohlcv = self.ctx.ohlcv
        num_bars = self.ctx.num_bars

        # 生成唯一调用key（用于维护指标状态）
        self.ctx._ta_call_counter += 1
        call_key = f"{name}_{self.ctx._ta_call_counter}"

        # ---- 类型转换函数 ----
        if name == "int":
            return self._safe_int(args[0] if args else 0)
        if name == "float":
            return self._safe_float(args[0] if args else 0.0)
        if name == "str" or name == "str.tostring":
            return str(args[0]) if args else ""
        if name == "bool":
            return bool(args[0]) if args else False

        # ---- indicator / strategy 声明 ----
        if name == "indicator":
            self.ctx.strategy_mode = False
            return None
        if name == "strategy":
            self.ctx.strategy_mode = True
            if args:
                self.ctx.strategy_name = str(args[0])
            return None

        # ---- input 函数 ----
        if name in ("input", "input.int", "input.float", "input.bool", "input.string", "input.source"):
            return self._handle_input(name, args, kwargs)

        # ---- plot 绘图函数 ----
        if name == "plot":
            return self._handle_plot(args, kwargs)
        if name == "plotshape":
            return self._handle_plotshape(args, kwargs)
        if name == "hline":
            return self._handle_hline(args, kwargs)
        if name == "fill":
            return None  # 简化处理
        if name == "bgcolor":
            return self._handle_bgcolor(args, kwargs)
        if name == "alertcondition":
            return self._handle_alertcondition(args, kwargs)

        # ---- strategy 函数 ----
        if name == "strategy.entry":
            return self._handle_strategy_entry(args, kwargs)
        if name == "strategy.close":
            return self._handle_strategy_close(args, kwargs)
        if name == "strategy.exit":
            return self._handle_strategy_exit(args, kwargs)

        # ---- ta.* 技术指标函数 ----
        # SMA
        if name in ("ta.sma", "sma"):
            return self._ta_sma(args, call_key)
        if name in ("ta.ema", "ema"):
            return self._ta_ema(args, call_key)
        if name in ("ta.wma", "wma"):
            return self._ta_wma(args, call_key)
        if name in ("ta.rsi", "rsi"):
            return self._ta_rsi(args, call_key)
        if name in ("ta.macd", "macd"):
            return self._ta_macd(args, call_key)
        if name in ("ta.stdev", "stdev"):
            return self._ta_stdev(args, call_key)
        if name in ("ta.highest", "highest"):
            return self._ta_highest(args, call_key)
        if name in ("ta.lowest", "lowest"):
            return self._ta_lowest(args, call_key)
        if name in ("ta.atr",):
            return self._ta_atr(args, call_key)
        if name in ("ta.crossover", "crossover"):
            return self._ta_crossover(args, call_key)
        if name in ("ta.crossunder", "crossunder"):
            return self._ta_crossunder(args, call_key)
        if name in ("ta.bb",):
            return self._ta_bb(args, call_key)
        if name in ("ta.change",):
            return self._ta_change(args, call_key)
        if name in ("ta.mom",):
            return self._ta_mom(args, call_key)
        if name in ("ta.pivothigh",):
            return self._ta_pivothigh(args, call_key)
        if name in ("ta.pivotlow",):
            return self._ta_pivotlow(args, call_key)
        if name in ("ta.rising", "rising"):
            return self._ta_rising(args, call_key)
        if name in ("ta.falling", "falling"):
            return self._ta_falling(args, call_key)
        if name in ("ta.valuewhen", "valuewhen"):
            return self._ta_valuewhen(args, call_key)
        if name in ("ta.barssince", "barssince"):
            return self._ta_barssince(args, call_key)
        if name in ("ta.cum",):
            return self._ta_cum(args, call_key)
        if name in ("ta.tr",):
            return self._ta_tr(args, call_key)

        # ---- math.* 数学函数 ----
        if name in ("math.abs", "abs"):
            return abs(args[0]) if args[0] is not None else None
        if name in ("math.max", "max"):
            vals = [v for v in args if v is not None]
            return max(vals) if vals else None
        if name in ("math.min", "min"):
            vals = [v for v in args if v is not None]
            return min(vals) if vals else None
        if name in ("math.sqrt", "sqrt"):
            if args[0] is None or args[0] < 0:
                return None
            return math.sqrt(args[0])
        if name in ("math.log", "log"):
            if args[0] is None or args[0] <= 0:
                return None
            return math.log(args[0])
        if name in ("math.pow", "pow"):
            if args[0] is None or args[1] is None:
                return None
            return math.pow(args[0], args[1])
        if name == "math.ceil":
            return math.ceil(args[0]) if args[0] is not None else None
        if name == "math.floor":
            return math.floor(args[0]) if args[0] is not None else None
        if name == "math.round":
            return round(args[0]) if args[0] is not None else None
        if name == "math.sign":
            if args[0] is None:
                return None
            return 1 if args[0] > 0 else (-1 if args[0] < 0 else 0)
        if name == "math.avg":
            vals = [v for v in args if v is not None]
            return sum(vals) / len(vals) if vals else None
        if name == "math.sum":
            vals = [v for v in args if v is not None]
            return sum(vals)

        # ---- nz / na / fixnan ----
        if name == "nz":
            val = args[0] if args else None
            replacement = args[1] if len(args) > 1 else 0
            if val is None or _is_nan(val):
                return replacement
            return val
        if name == "na":
            if len(args) == 0:
                return None
            return args[0] is None or _is_nan(args[0])
        if name == "fixnan":
            return self._fixnan(args, call_key)

        # ---- array.* 数组函数 ----
        if name == "array.new_float":
            size = self._safe_int(args[0]) if args else 0
            init_val = float(args[1]) if len(args) > 1 else float("nan")
            return PineArray(size, init_val)
        if name == "array.new_int":
            size = self._safe_int(args[0]) if args else 0
            init_val = self._safe_int(args[1]) if len(args) > 1 else 0
            return PineArray(size, init_val)
        if name == "array.new_bool":
            size = self._safe_int(args[0]) if args else 0
            init_val = bool(args[1]) if len(args) > 1 else False
            return PineArray(size, init_val)
        if name == "array.new_string":
            size = self._safe_int(args[0]) if args else 0
            init_val = str(args[1]) if len(args) > 1 else ""
            return PineArray(size, init_val)
        if name == "array.push":
            if isinstance(args[0], PineArray):
                args[0].push(args[1] if len(args) > 1 else None)
            return None
        if name == "array.get":
            if isinstance(args[0], PineArray):
                return args[0].get(self._safe_int(args[1]))
            return None
        if name == "array.set":
            if isinstance(args[0], PineArray):
                args[0].set(self._safe_int(args[1]), args[2] if len(args) > 2 else None)
            return None
        if name == "array.size":
            if isinstance(args[0], PineArray):
                return args[0].size()
            return 0
        if name == "array.pop":
            if isinstance(args[0], PineArray):
                return args[0].pop()
            return None
        if name == "array.remove":
            if isinstance(args[0], PineArray):
                return args[0].remove(self._safe_int(args[1]))
            return None
        if name == "array.insert":
            if isinstance(args[0], PineArray):
                args[0].insert(self._safe_int(args[1]), args[2] if len(args) > 2 else None)
            return None
        if name == "array.clear":
            if isinstance(args[0], PineArray):
                args[0].clear()
            return None
        if name == "array.copy":
            if isinstance(args[0], PineArray):
                return args[0].copy()
            return PineArray()
        if name == "array.sum":
            if isinstance(args[0], PineArray):
                return args[0].sum()
            return 0
        if name == "array.avg":
            if isinstance(args[0], PineArray):
                return args[0].avg()
            return None
        if name == "array.min":
            if isinstance(args[0], PineArray):
                return args[0].min()
            return None
        if name == "array.max":
            if isinstance(args[0], PineArray):
                return args[0].max()
            return None
        if name == "array.includes":
            if isinstance(args[0], PineArray):
                return args[0].includes(args[1] if len(args) > 1 else None)
            return False
        if name == "array.unshift":
            if isinstance(args[0], PineArray):
                args[0].unshift(args[1] if len(args) > 1 else None)
            return None
        if name == "array.shift":
            if isinstance(args[0], PineArray):
                return args[0].shift()
            return None
        if name == "array.slice":
            if isinstance(args[0], PineArray):
                start = self._safe_int(args[1]) if len(args) > 1 else 0
                end = self._safe_int(args[2]) if len(args) > 2 else None
                return args[0].slice(start, end)
            return PineArray()

        # ---- str.* 字符串函数 ----
        if name == "str.tostring":
            return str(args[0]) if args else ""
        if name == "str.format":
            if args:
                fmt = args[0]
                return fmt.format(*args[1:]) if len(args) > 1 else fmt
            return ""
        if name == "str.contains":
            if len(args) >= 2:
                return str(args[1]) in str(args[0])
            return False
        if name == "str.length":
            return len(str(args[0])) if args else 0

        # ---- color.* ----
        if name == "color.new":
            # color.new(base_color, transp) -> 返回颜色字符串
            base = args[0] if args else "#2196F3"
            if isinstance(base, _BuiltinNamespace):
                base = self._resolve_color(base.name)
            return base if isinstance(base, str) else "#2196F3"
        if name.startswith("color."):
            return self._resolve_color(name)

        # ---- request.security ----
        if name == "request.security":
            # stub: 返回第3个参数的当前值（expression），或0
            if len(args) >= 3:
                return args[2]
            return 0

        # ---- line/label/box 绘图对象 ----
        if name == "line.new":
            obj_id = f"line_{len(self.ctx._drawings)}"
            raw_color = kwargs.get("color", args[4] if len(args) > 4 else "#888888")
            drawing = {
                "type": "line",
                "id": obj_id,
                "x1": self._safe_int(args[0] if len(args) > 0 else 0),
                "y1": self._safe_float(args[1] if len(args) > 1 else 0),
                "x2": self._safe_int(args[2] if len(args) > 2 else 0),
                "y2": self._safe_float(args[3] if len(args) > 3 else 0),
                "color": self._resolve_color(raw_color),
                "width": self._safe_int(kwargs.get("width", args[5] if len(args) > 5 else 1)),
            }
            self.ctx._drawings.append(drawing)
            return obj_id
        if name == "label.new":
            obj_id = f"label_{len(self.ctx._drawings)}"
            drawing = {
                "type": "label",
                "id": obj_id,
                "x": self._safe_int(args[0] if len(args) > 0 else 0),
                "y": self._safe_float(args[1] if len(args) > 1 else 0),
                "text": str(args[2] if len(args) > 2 else kwargs.get("text", "")),
                "color": self._resolve_color(kwargs.get("color", kwargs.get("textcolor", "#FFFFFF"))),
                "style": str(kwargs.get("style", args[3] if len(args) > 3 else "label_down")),
                "size": str(kwargs.get("size", "normal")),
            }
            self.ctx._drawings.append(drawing)
            return obj_id
        if name == "box.new":
            obj_id = f"box_{len(self.ctx._drawings)}"
            drawing = {
                "type": "box",
                "id": obj_id,
                "left": self._safe_int(args[0] if len(args) > 0 else 0),
                "top": self._safe_float(args[1] if len(args) > 1 else 0),
                "right": self._safe_int(args[2] if len(args) > 2 else 0),
                "bottom": self._safe_float(args[3] if len(args) > 3 else 0),
                "border_color": self._resolve_color(
                    kwargs.get("border_color", args[4] if len(args) > 4 else "#888888")
                ),
                "bgcolor": self._resolve_color(kwargs.get("bgcolor", args[5] if len(args) > 5 else "rgba(0,0,0,0)")),
            }
            self.ctx._drawings.append(drawing)
            return obj_id
        if name in (
            "line.set_xy1",
            "line.set_xy2",
            "line.set_x1",
            "line.set_y1",
            "line.set_x2",
            "line.set_y2",
            "line.set_color",
            "line.set_width",
            "line.delete",
            "label.set_xy",
            "label.set_x",
            "label.set_y",
            "label.set_text",
            "label.set_color",
            "label.set_textcolor",
            "label.delete",
            "box.set_lefttop",
            "box.set_rightbottom",
            "box.set_left",
            "box.set_top",
            "box.set_right",
            "box.set_bottom",
            "box.delete",
            "line.set_style",
            "label.set_style",
            "box.set_border_color",
            "box.set_bgcolor",
            "table.new",
            "table.cell",
            "table.delete",
        ):
            return None
        if (
            name.startswith("line.")
            or name.startswith("label.")
            or name.startswith("box.")
            or name.startswith("table.")
        ):
            return None

        # 未知函数：返回 None
        return None

    # ---- 技术指标实现（逐bar计算） ----

    def _get_source_value(self, source_arg) -> float:
        """解析source参数，获取当前bar的值"""
        if source_arg is None:
            return None
        if isinstance(source_arg, (int, float)):
            return float(source_arg)
        if isinstance(source_arg, bool):
            return float(source_arg)
        return None

    def _ta_sma(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 14
        if length < 1:
            return None

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) < length:
            return None

        window = history[-length:]
        valid = [v for v in window if v is not None]
        if len(valid) < length:
            return None
        return sum(valid) / length

    def _ta_ema(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 14
        if length < 1:
            return None

        history = self.ctx.ta_state.get_history(key)
        state = self.ctx.ta_state.get_state(key)
        val = self._get_source_value(source)
        history.append(val)

        if val is None:
            return state.get("prev_ema")

        if "prev_ema" not in state:
            # 需要足够的数据来初始化
            valid_vals = [v for v in history if v is not None]
            if len(valid_vals) >= length:
                # 用前 length 个有效值的 SMA 作为初始 EMA
                initial_sma = sum(valid_vals[:length]) / length
                state["prev_ema"] = initial_sma
                # 对后续值应用 EMA
                alpha = 2.0 / (length + 1)
                ema_val = initial_sma
                for v in valid_vals[length:]:
                    ema_val = alpha * v + (1 - alpha) * ema_val
                # 当前值
                ema_val = alpha * val + (1 - alpha) * ema_val
                state["prev_ema"] = ema_val
                return ema_val
            return None
        else:
            alpha = 2.0 / (length + 1)
            ema_val = alpha * val + (1 - alpha) * state["prev_ema"]
            state["prev_ema"] = ema_val
            return ema_val

    def _ta_wma(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 14

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) < length:
            return None
        window = history[-length:]
        if any(v is None for v in window):
            return None
        weights = list(range(1, length + 1))
        w_sum = sum(weights)
        return sum(w * v for w, v in zip(weights, window)) / w_sum

    def _ta_rsi(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 14

        history = self.ctx.ta_state.get_history(key)
        state = self.ctx.ta_state.get_state(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) < 2:
            return None

        if val is None or history[-2] is None:
            return state.get("prev_rsi")

        delta = val - history[-2]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0

        if "avg_gain" not in state:
            # 收集初始期间的gain/loss
            if "gains" not in state:
                state["gains"] = []
                state["losses"] = []
            state["gains"].append(gain)
            state["losses"].append(loss)

            if len(state["gains"]) >= length:
                avg_gain = sum(state["gains"][:length]) / length
                avg_loss = sum(state["losses"][:length]) / length
                state["avg_gain"] = avg_gain
                state["avg_loss"] = avg_loss
                # 对后续数据应用平滑
                for i in range(length, len(state["gains"])):
                    state["avg_gain"] = (state["avg_gain"] * (length - 1) + state["gains"][i]) / length
                    state["avg_loss"] = (state["avg_loss"] * (length - 1) + state["losses"][i]) / length
                del state["gains"]
                del state["losses"]

                if state["avg_loss"] == 0:
                    rsi_val = 100.0
                else:
                    rs = state["avg_gain"] / state["avg_loss"]
                    rsi_val = 100.0 - 100.0 / (1.0 + rs)
                state["prev_rsi"] = rsi_val
                return rsi_val
            return None
        else:
            state["avg_gain"] = (state["avg_gain"] * (length - 1) + gain) / length
            state["avg_loss"] = (state["avg_loss"] * (length - 1) + loss) / length
            if state["avg_loss"] == 0:
                rsi_val = 100.0
            else:
                rs = state["avg_gain"] / state["avg_loss"]
                rsi_val = 100.0 - 100.0 / (1.0 + rs)
            state["prev_rsi"] = rsi_val
            return rsi_val

    def _ta_macd(self, args, key) -> Any:
        source = args[0] if args else None
        fast_len = self._safe_int(args[1]) if len(args) > 1 else 12
        slow_len = self._safe_int(args[2]) if len(args) > 2 else 26
        sig_len = self._safe_int(args[3]) if len(args) > 3 else 9

        fast_key = f"{key}_fast"
        slow_key = f"{key}_slow"
        sig_key = f"{key}_sig"

        fast_ema = self._ta_ema([source, fast_len], fast_key)
        slow_ema = self._ta_ema([source, slow_len], slow_key)

        if fast_ema is None or slow_ema is None:
            # 仍然需要feed signal ema
            self._ta_ema([None, sig_len], sig_key)
            return [None, None, None]

        macd_val = fast_ema - slow_ema
        signal = self._ta_ema([macd_val, sig_len], sig_key)

        if signal is None:
            return [macd_val, None, None]

        hist = (macd_val - signal) * 2
        return [macd_val, signal, hist]

    def _ta_stdev(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 14

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) < length:
            return None
        window = history[-length:]
        valid = [v for v in window if v is not None]
        if len(valid) < length:
            return None
        mean = sum(valid) / len(valid)
        variance = sum((v - mean) ** 2 for v in valid) / len(valid)
        return math.sqrt(variance)

    def _ta_highest(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 14

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) < length:
            return None
        window = history[-length:]
        valid = [v for v in window if v is not None]
        return max(valid) if valid else None

    def _ta_lowest(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 14

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) < length:
            return None
        window = history[-length:]
        valid = [v for v in window if v is not None]
        return min(valid) if valid else None

    def _ta_atr(self, args, key) -> Any:
        length = self._safe_int(args[0]) if args else 14
        bar_idx = self.ctx.bar_index
        ohlcv = self.ctx.ohlcv

        high_val = float(ohlcv["high"][bar_idx])
        low_val = float(ohlcv["low"][bar_idx])

        if bar_idx == 0:
            tr = high_val - low_val
        else:
            prev_close = float(ohlcv["close"][bar_idx - 1])
            tr = max(high_val - low_val, abs(high_val - prev_close), abs(low_val - prev_close))

        return self._ta_ema([tr, length], f"{key}_ema")

    def _ta_crossover(self, args, key) -> bool:
        a = args[0] if args else None
        b = args[1] if len(args) > 1 else None

        state = self.ctx.ta_state.get_state(key)
        prev_a = state.get("prev_a")
        prev_b = state.get("prev_b")
        state["prev_a"] = a
        state["prev_b"] = b

        if a is None or b is None or prev_a is None or prev_b is None:
            return False
        return a > b and prev_a <= prev_b

    def _ta_crossunder(self, args, key) -> bool:
        a = args[0] if args else None
        b = args[1] if len(args) > 1 else None

        state = self.ctx.ta_state.get_state(key)
        prev_a = state.get("prev_a")
        prev_b = state.get("prev_b")
        state["prev_a"] = a
        state["prev_b"] = b

        if a is None or b is None or prev_a is None or prev_b is None:
            return False
        return a < b and prev_a >= prev_b

    def _ta_bb(self, args, key) -> list:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 20
        mult = float(args[2]) if len(args) > 2 else 2.0

        middle = self._ta_sma([source, length], f"{key}_sma")
        sd = self._ta_stdev([source, length], f"{key}_std")

        if middle is None or sd is None:
            return [None, None, None]

        upper = middle + mult * sd
        lower = middle - mult * sd
        return [middle, upper, lower]

    def _ta_change(self, args, key) -> Any:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 1

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) <= length:
            return None
        prev = history[-length - 1]
        if val is None or prev is None:
            return None
        return val - prev

    def _ta_mom(self, args, key) -> Any:
        # 动量 = 当前值 - n周期前的值
        return self._ta_change(args, key)

    def _ta_pivothigh(self, args, key) -> Any:
        source = args[0] if args else None
        leftbars = self._safe_int(args[1]) if len(args) > 1 else 5
        rightbars = self._safe_int(args[2]) if len(args) > 2 else 5

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        total = leftbars + rightbars + 1
        if len(history) < total:
            return None

        pivot_idx = len(history) - 1 - rightbars
        pivot_val = history[pivot_idx]
        if pivot_val is None:
            return None

        for i in range(pivot_idx - leftbars, pivot_idx):
            if history[i] is None or history[i] >= pivot_val:
                return None
        for i in range(pivot_idx + 1, pivot_idx + rightbars + 1):
            if history[i] is None or history[i] >= pivot_val:
                return None
        return pivot_val

    def _ta_pivotlow(self, args, key) -> Any:
        source = args[0] if args else None
        leftbars = self._safe_int(args[1]) if len(args) > 1 else 5
        rightbars = self._safe_int(args[2]) if len(args) > 2 else 5

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        total = leftbars + rightbars + 1
        if len(history) < total:
            return None

        pivot_idx = len(history) - 1 - rightbars
        pivot_val = history[pivot_idx]
        if pivot_val is None:
            return None

        for i in range(pivot_idx - leftbars, pivot_idx):
            if history[i] is None or history[i] <= pivot_val:
                return None
        for i in range(pivot_idx + 1, pivot_idx + rightbars + 1):
            if history[i] is None or history[i] <= pivot_val:
                return None
        return pivot_val

    def _ta_rising(self, args, key) -> bool:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 1

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) <= length:
            return False
        for i in range(1, length + 1):
            idx = len(history) - i
            prev_idx = idx - 1
            if prev_idx < 0:
                return False
            if history[idx] is None or history[prev_idx] is None:
                return False
            if history[idx] <= history[prev_idx]:
                return False
        return True

    def _ta_falling(self, args, key) -> bool:
        source = args[0] if args else None
        length = self._safe_int(args[1]) if len(args) > 1 else 1

        history = self.ctx.ta_state.get_history(key)
        val = self._get_source_value(source)
        history.append(val)

        if len(history) <= length:
            return False
        for i in range(1, length + 1):
            idx = len(history) - i
            prev_idx = idx - 1
            if prev_idx < 0:
                return False
            if history[idx] is None or history[prev_idx] is None:
                return False
            if history[idx] >= history[prev_idx]:
                return False
        return True

    def _ta_valuewhen(self, args, key) -> Any:
        condition = args[0] if args else False
        source = args[1] if len(args) > 1 else None
        occurrence = self._safe_int(args[2]) if len(args) > 2 else 0

        state = self.ctx.ta_state.get_state(key)
        if "cond_vals" not in state:
            state["cond_vals"] = []

        if self._is_truthy(condition):
            val = self._get_source_value(source)
            state["cond_vals"].append(val)

        vals = state["cond_vals"]
        if len(vals) > occurrence:
            return vals[-(occurrence + 1)]
        return None

    def _ta_barssince(self, args, key) -> Any:
        condition = args[0] if args else False
        state = self.ctx.ta_state.get_state(key)

        if self._is_truthy(condition):
            state["last_true_bar"] = self.ctx.bar_index

        if "last_true_bar" in state:
            return self.ctx.bar_index - state["last_true_bar"]
        return None

    def _ta_cum(self, args, key) -> Any:
        source = args[0] if args else None
        state = self.ctx.ta_state.get_state(key)
        val = self._get_source_value(source)
        if val is not None:
            state["sum"] = state.get("sum", 0) + val
        return state.get("sum", 0)

    def _ta_tr(self, args, key) -> Any:
        bar_idx = self.ctx.bar_index
        ohlcv = self.ctx.ohlcv
        high_val = float(ohlcv["high"][bar_idx])
        low_val = float(ohlcv["low"][bar_idx])
        if bar_idx == 0:
            return high_val - low_val
        prev_close = float(ohlcv["close"][bar_idx - 1])
        return max(high_val - low_val, abs(high_val - prev_close), abs(low_val - prev_close))

    def _fixnan(self, args, key) -> Any:
        val = args[0] if args else None
        state = self.ctx.ta_state.get_state(key)
        if val is not None and not _is_nan(val):
            state["last_valid"] = val
            return val
        return state.get("last_valid")

    # ---- 绘图函数实现 ----

    def _handle_plot(self, args, kwargs) -> int:
        """plot() — 每个bar记录一个值，最终组装成序列"""
        series_val = args[0] if args else None
        title = kwargs.get("title", args[1] if len(args) > 1 else "")
        if isinstance(title, _BuiltinNamespace):
            title = ""
        color = kwargs.get("color", args[2] if len(args) > 2 else "#2196F3")
        color = self._resolve_color_value(color)
        linewidth = kwargs.get("linewidth", args[3] if len(args) > 3 else 1)
        style = kwargs.get("style", args[4] if len(args) > 4 else "line")

        # 首次调用此plot时分配ID
        call_id = self.ctx._ta_call_counter

        if call_id not in self.ctx._plot_configs:
            self.ctx._plot_configs[call_id] = {
                "type": "plot",
                "title": title if isinstance(title, str) else str(title) if title else "",
                "color": color if isinstance(color, str) else "#2196F3",
                "linewidth": linewidth if isinstance(linewidth, (int, float)) else 1,
                "style": style if isinstance(style, str) else "line",
            }
            self.ctx._plot_series[call_id] = []

        val = self._get_source_value(series_val)
        self.ctx._plot_series[call_id].append(val)
        return call_id

    def _handle_plotshape(self, args, kwargs):
        condition = args[0] if args else None

        call_id = self.ctx._ta_call_counter
        if call_id not in self.ctx._shape_configs:
            title = kwargs.get("title", "")
            style_val = kwargs.get("style", "triangleup")
            location = kwargs.get("location", "belowbar")
            color = kwargs.get("color", "#4CAF50")
            color = self._resolve_color_value(color)
            text = kwargs.get("text", "")

            self.ctx._shape_configs[call_id] = {
                "type": "plotshape",
                "title": title if isinstance(title, str) else "",
                "style": style_val if isinstance(style_val, str) else "triangleup",
                "location": location if isinstance(location, str) else "belowbar",
                "color": color if isinstance(color, str) else "#4CAF50",
                "text": text if isinstance(text, str) else "",
            }
            self.ctx._shape_series[call_id] = []

        triggered = self._is_truthy(condition)
        self.ctx._shape_series[call_id].append(triggered)

    def _handle_hline(self, args, kwargs):
        price = args[0] if args else 0
        if self.ctx.bar_index == 0:  # 只记录一次
            title = kwargs.get("title", args[1] if len(args) > 1 else "")
            color = kwargs.get("color", "#787878")
            color = self._resolve_color_value(color)
            linestyle = kwargs.get("linestyle", "dashed")

            self.ctx._hlines.append(
                {
                    "type": "hline",
                    "price": price,
                    "title": title if isinstance(title, str) else "",
                    "color": color if isinstance(color, str) else "#787878",
                    "linestyle": linestyle if isinstance(linestyle, str) else "dashed",
                    "linewidth": 1,
                }
            )

    def _handle_bgcolor(self, args, kwargs):
        condition = args[0] if args else None
        call_id = self.ctx._ta_call_counter

        if call_id not in self.ctx._shape_configs:
            color = kwargs.get("color", "rgba(76,175,80,0.1)")
            color = self._resolve_color_value(color)
            self.ctx._shape_configs[call_id] = {
                "type": "bgcolor",
                "color": color if isinstance(color, str) else "rgba(76,175,80,0.1)",
            }
            self.ctx._shape_series[call_id] = []

        triggered = self._is_truthy(condition)
        self.ctx._shape_series[call_id].append(triggered)

    def _handle_alertcondition(self, args, kwargs):
        condition = args[0] if args else None
        call_id = self.ctx._ta_call_counter

        if call_id not in self.ctx._alert_configs:
            title = kwargs.get("title", "")
            message = kwargs.get("message", "")
            self.ctx._alert_configs[call_id] = {
                "type": "alertcondition",
                "title": title if isinstance(title, str) else "",
                "message": message if isinstance(message, str) else "",
            }
            self.ctx._alert_series[call_id] = []

        triggered = self._is_truthy(condition)
        self.ctx._alert_series[call_id].append(triggered)

    # ---- strategy 函数 ----

    def _handle_strategy_entry(self, args, kwargs):
        entry_id = args[0] if args else "entry"
        direction = args[1] if len(args) > 1 else "long"
        qty = kwargs.get("qty", args[2] if len(args) > 2 else 1.0)

        # 解析 direction
        if isinstance(direction, _BuiltinNamespace):
            dir_name = direction.name
            if "long" in dir_name:
                direction = "long"
            elif "short" in dir_name:
                direction = "short"
            else:
                direction = "long"

        self.ctx.orders.append(
            {
                "action": "entry",
                "id": str(entry_id),
                "direction": str(direction),
                "qty": float(qty) if isinstance(qty, (int, float)) else 1.0,
                "bar_index": self.ctx.bar_index,
                "when": True,
            }
        )

    def _handle_strategy_close(self, args, kwargs):
        close_id = args[0] if args else "entry"
        self.ctx.orders.append(
            {
                "action": "close",
                "id": str(close_id),
                "bar_index": self.ctx.bar_index,
                "when": True,
            }
        )

    def _handle_strategy_exit(self, args, kwargs):
        exit_id = args[0] if args else "exit"
        from_entry = kwargs.get("from_entry", "")
        stop = kwargs.get("stop", 0)
        limit = kwargs.get("limit", 0)
        self.ctx.orders.append(
            {
                "action": "exit",
                "id": str(exit_id),
                "from_entry": str(from_entry),
                "stop": float(stop) if isinstance(stop, (int, float)) else 0,
                "limit": float(limit) if isinstance(limit, (int, float)) else 0,
                "bar_index": self.ctx.bar_index,
                "when": True,
            }
        )

    # ---- input 处理 ----

    def _handle_input(self, func_name, args, kwargs):
        # 如果用户提供了参数覆盖，使用用户参数
        # 需要匹配参数名（来自解析阶段提取的 input 信息）
        # 先检查默认值
        default = args[0] if args else None
        if "defval" in kwargs:
            default = kwargs["defval"]

        # 处理 input.source
        if func_name == "input.source":
            if default is not None:
                if isinstance(default, _BuiltinNamespace):
                    source_name = default.name
                    if source_name in self.ctx.ohlcv:
                        return float(self.ctx.ohlcv[source_name][self.ctx.bar_index])
                return self._get_source_value(default)
            return float(self.ctx.ohlcv["close"][self.ctx.bar_index])

        # 检查是否有用户参数覆盖
        # (简化处理：直接返回默认值，用户参数在上下文初始化时已注入)
        if default is not None and isinstance(default, _BuiltinNamespace):
            # close, open 等作为 source 参数
            source_name = default.name
            if source_name in self.ctx.ohlcv:
                return float(self.ctx.ohlcv[source_name][self.ctx.bar_index])

        return default

    # ---- 颜色解析 ----

    def _resolve_color(self, val):
        """将颜色值解析为字符串"""
        if isinstance(val, str):
            return val
        if isinstance(val, _BuiltinNamespace):
            return self._COLOR_MAP.get(val.name, "#888888")
        if val is None:
            return "#888888"
        return str(val)

    _COLOR_MAP = {
        "color.green": "#4CAF50",
        "color.red": "#F44336",
        "color.blue": "#2196F3",
        "color.orange": "#FF9800",
        "color.yellow": "#FFEB3B",
        "color.purple": "#9C27B0",
        "color.white": "#FFFFFF",
        "color.black": "#000000",
        "color.gray": "#9E9E9E",
        "color.silver": "#BDBDBD",
        "color.aqua": "#00BCD4",
        "color.lime": "#8BC34A",
        "color.maroon": "#880E4F",
        "color.navy": "#1A237E",
        "color.olive": "#827717",
        "color.teal": "#009688",
        "color.fuchsia": "#E040FB",
    }

    # 旧版兼容方法（已统一到1773行的_resolve_color）
    def _resolve_color_value(self, val) -> str:
        return self._resolve_color(val)

    # ---- 输出组装 ----

    def _finalize_outputs(self):
        """将逐bar收集的数据组装成最终输出"""
        # plots
        for plot_id, config in self.ctx._plot_configs.items():
            data = self.ctx._plot_series.get(plot_id, [])
            # 转换 None 为 NaN-safe 的列表
            cleaned = []
            for v in data:
                if v is None or _is_nan(v):
                    cleaned.append(None)
                elif isinstance(v, (int, float)):
                    cleaned.append(float(v))
                else:
                    cleaned.append(None)
            self.ctx.plots.append({**config, "data": cleaned})

        # hlines
        for hline_info in self.ctx._hlines:
            self.ctx.plots.append(hline_info)

        # shapes
        for shape_id, config in self.ctx._shape_configs.items():
            data = self.ctx._shape_series.get(shape_id, [])
            self.ctx.shapes.append({**config, "data": data})

        # alerts
        for alert_id, config in self.ctx._alert_configs.items():
            data = self.ctx._alert_series.get(alert_id, [])
            self.ctx.alerts.append({**config, "data": data})

    # ---- 辅助函数 ----

    @staticmethod
    def _is_truthy(val) -> bool:
        if val is None:
            return False
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            if _is_nan(val):
                return False
            return val != 0
        if isinstance(val, str):
            return len(val) > 0
        if isinstance(val, _BuiltinNamespace):
            return False
        return bool(val)


# ============================================================
# 辅助类型
# ============================================================


class _BuiltinNamespace:
    """内置命名空间标识（ta, math, array, strategy, color 等）"""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"<Namespace:{self.name}>"

    def __eq__(self, other):
        if isinstance(other, _BuiltinNamespace):
            return self.name == other.name
        return False


class _BoundMethod:
    """PineArray 的绑定方法"""

    def __init__(self, obj, method: str):
        self.obj = obj
        self.method = method


class _UserFunction:
    """用户定义函数"""

    def __init__(self, name: str, params: list[str], body: list):
        self.name = name
        self.params = params
        self.body = body


# ============================================================
# 公开接口
# ============================================================


def execute_openscript(
    code: str,
    ohlcv_data: dict[str, list | np.ndarray],
    user_params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict:
    """
    执行 Pine Script / OpenScript 代码。

    Args:
        code: 源代码
        ohlcv_data: OHLCV 数据字典
        user_params: 用户自定义参数值
        timeout: 执行超时时间（秒）

    Returns:
        {
            "plots": [...],
            "shapes": [...],
            "alerts": [...],
            "orders": [...],
            "inputs": [...],
            "meta": {...}
        }
    """
    # 转换数据为 numpy 数组
    ohlcv = {}
    for key in ("open", "high", "low", "close", "volume"):
        data = ohlcv_data.get(key, [])
        if isinstance(data, np.ndarray):
            ohlcv[key] = data.astype(np.float64)
        else:
            ohlcv[key] = np.array(data, dtype=np.float64)

    if len(ohlcv.get("close", [])) == 0:
        return {
            "plots": [],
            "shapes": [],
            "alerts": [],
            "orders": [],
            "inputs": [],
            "meta": {"mode": "indicator", "name": "", "overlay": False},
        }

    # 解析代码
    try:
        parse_result = parse_openscript(code, user_params)
    except OpenScriptError as e:
        raise ExecutionError(f"解析失败: {e}") from e

    # 安全检查
    _check_forbidden(code)

    # 创建执行上下文
    ctx = InterpreterContext(ohlcv, user_params)
    ctx._timeout = timeout
    ctx.strategy_mode = parse_result.mode == "strategy"

    # 注入用户参数覆盖 input 默认值
    if user_params:
        for inp in parse_result.inputs:
            if inp.name in user_params:
                ctx.variables[inp.name] = user_params[inp.name]

    # 在超时限制内执行
    result_holder = [None]
    error_holder = [None]

    def run():
        try:
            interpreter = PineInterpreter(ctx)
            interpreter.run(parse_result.ast)
        except _TimeoutError as e:
            error_holder[0] = e
        except Exception as e:
            error_holder[0] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        raise ExecutionError(f"代码执行超时 ({timeout}秒)")

    if error_holder[0] is not None:
        err = error_holder[0]
        if isinstance(err, _TimeoutError):
            raise ExecutionError(str(err))
        raise ExecutionError(f"执行失败: {type(err).__name__}: {err}") from err

    # 组装结果
    result = {
        "plots": ctx.plots,
        "shapes": ctx.shapes,
        "alerts": ctx.alerts,
        "orders": ctx.orders,
        "drawings": ctx._drawings,
        "inputs": [inp.to_dict() for inp in parse_result.inputs],
        "meta": {
            "mode": parse_result.mode,
            "name": parse_result.name,
            "overlay": parse_result.overlay,
        },
    }

    if parse_result.mode == "strategy":
        result["meta"]["initial_capital"] = parse_result.initial_capital

    return result


def validate_and_preview(
    code: str,
    ohlcv_data: dict[str, list | np.ndarray] | None = None,
) -> dict:
    """
    验证代码并返回预览信息（不执行）。
    """
    errors = []
    inputs = []
    meta = {}

    try:
        parse_result = parse_openscript(code)
        inputs = [inp.to_dict() for inp in parse_result.inputs]
        meta = {
            "mode": parse_result.mode,
            "name": parse_result.name,
            "overlay": parse_result.overlay,
        }
        _check_forbidden(code)
    except (OpenScriptError, ExecutionError) as e:
        errors.append(str(e))
    except Exception as e:
        errors.append(f"未知错误: {e}")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "inputs": inputs,
        "meta": meta,
    }


# ============================================================
# 安全检查
# ============================================================

_FORBIDDEN_PATTERNS = [
    "__import__",
    "import ",
    "exec(",
    "eval(",
    "compile(",
    "globals(",
    "locals(",
    "getattr(",
    "setattr(",
    "delattr(",
    "breakpoint(",
    "__builtins__",
    "__class__",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "subprocess",
    "os.system",
    "os.popen",
    "shutil",
]


def _check_forbidden(code: str) -> None:
    """检查代码中是否包含危险模式"""
    code_lower = code.lower()
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern.lower() in code_lower:
            # 排除正常的 Pine Script 用法
            if pattern == "import " and "import " in code_lower:
                # Pine Script 不支持 import，但作为错误处理而非安全问题
                continue
            raise ExecutionError(f"代码包含禁止的操作: {pattern}")
