"""安全的数学表达式求值（M32 函数绘图基础设施）。

设计红线（对应《技术文档》§6 安全边界：**不执行模型给的代码**）：

* **不用 ``eval`` / ``exec``**：自己递归解释 AST；
* 只放行 ``Constant / Name / BinOp / UnaryOp / Compare / IfExp / Call``，
  属性访问（``x.__class__``）、下标（``x[0]``）、lambda、推导式、导入一律拒绝；
* 函数表固定白名单，变量名由调用方显式声明；
* 长度、AST 节点数、``**`` 指数都有上限，防止「一行表达式把内存吃干」。

numpy 走**函数内懒导入**：模块导入不付 numpy 的启动成本，缺依赖时抛出结构化错误，
而不是让应用起不来。
"""

from __future__ import annotations

import ast
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

MAX_EXPR_LENGTH = 200
MAX_AST_NODES = 120
MAX_POW_EXPONENT = 64.0
DEFAULT_VARIABLES: tuple[str, ...] = ("x",)

ALLOWED_CONSTANTS: frozenset[str] = frozenset({"pi", "e", "tau"})

_CONSTANT_VALUES: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


class ExprError(ValueError):
    """表达式不合法（语法 / 白名单 / 超限）或求值失败。"""


def _np() -> Any:
    """懒导入 numpy；缺失时给出一条人能看懂的错误。"""
    try:
        import numpy  # noqa: PLC0415 - 故意放在函数内：不进应用启动路径
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都降级为结构化错误
        raise ExprError("缺少 numpy 依赖，无法绘制函数图像") from exc
    return numpy


@lru_cache(maxsize=1)
def _functions() -> dict[str, tuple[tuple[int, ...], Any]]:
    """函数白名单：``名字 -> (允许的参数个数, 实现)``。

    实现一律走 numpy，因此标量与 ``ndarray`` 都能算（逐元素）。
    """
    np = _np()

    def logn(x: Any, base: Any = None) -> Any:
        return np.log(x) if base is None else np.log(x) / np.log(base)

    def clip(x: Any, low: Any, high: Any) -> Any:
        return np.clip(x, low, high)

    def hypot(*args: Any) -> Any:
        out = np.abs(args[0])
        for other in args[1:]:
            out = np.hypot(out, other)
        return out

    def fold(reducer: Any) -> Any:
        def inner(*args: Any) -> Any:
            out = args[0]
            for other in args[1:]:
                out = reducer(out, other)
            return out

        return inner

    def to_rad(degrees: Any) -> Any:
        return np.deg2rad(degrees)

    def to_deg(radians: Any) -> Any:
        return np.rad2deg(radians)

    return {
        "sin": ((1,), np.sin),
        "cos": ((1,), np.cos),
        "tan": ((1,), np.tan),
        "asin": ((1,), np.arcsin),
        "acos": ((1,), np.arccos),
        "atan": ((1,), np.arctan),
        "atan2": ((2,), np.arctan2),
        "sinh": ((1,), np.sinh),
        "cosh": ((1,), np.cosh),
        "tanh": ((1,), np.tanh),
        "exp": ((1,), np.exp),
        "log": ((1, 2), logn),
        "log2": ((1,), np.log2),
        "log10": ((1,), np.log10),
        "sqrt": ((1,), np.sqrt),
        "cbrt": ((1,), np.cbrt),
        "abs": ((1,), np.abs),
        "sign": ((1,), np.sign),
        "floor": ((1,), np.floor),
        "ceil": ((1,), np.ceil),
        "clip": ((3,), clip),
        "hypot": ((2, 3, 4), hypot),
        "mod": ((2,), np.mod),
        "deg": ((1,), to_deg),
        "rad": ((1,), to_rad),
        "min": ((2, 3, 4), fold(np.minimum)),
        "max": ((2, 3, 4), fold(np.maximum)),
        "pow": ((2,), np.power),
    }


def allowed_functions() -> tuple[str, ...]:
    """可用函数名（排序后的元组），供提示词与错误信息展示。"""
    return tuple(sorted(_functions()))


# --- 解析与校验 -----------------------------------------------------------
def _parse(expr: str) -> ast.Expression:
    text = str(expr or "").strip()
    if not text:
        raise ExprError("表达式不能为空")
    if len(text) > MAX_EXPR_LENGTH:
        raise ExprError(f"表达式过长（上限 {MAX_EXPR_LENGTH} 字符）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"表达式语法错误：{exc.msg}") from exc
    nodes = sum(1 for _ in ast.walk(tree))
    if nodes > MAX_AST_NODES:
        raise ExprError(f"表达式过于复杂（AST 节点上限 {MAX_AST_NODES}）")
    return tree


class _Evaluator:
    """递归解释器：只认识白名单里的节点类型，其余一律判不合法。"""

    def __init__(self, variables: dict[str, Any], functions: dict[str, tuple[tuple[int, ...], Any]]) -> None:
        self.variables = variables
        self.functions = functions

    def run(self, node: ast.AST) -> Any:
        return self._eval(node)

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ExprError(f"不支持的常量：{value!r}")
            return float(value)

        if isinstance(node, ast.Name):
            if node.id in self.variables:
                return self.variables[node.id]
            if node.id in _CONSTANT_VALUES:
                return _CONSTANT_VALUES[node.id]
            raise ExprError(f"不允许的名字：{node.id}（变量只有 {'、'.join(self.variables)}，常量只有 pi / e / tau）")

        if isinstance(node, ast.BinOp):
            return self._binop(node)

        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.Not):
                return _np().logical_not(value)
            raise ExprError(f"不允许的一元运算：{type(node.op).__name__}")

        if isinstance(node, ast.Compare):
            return self._compare(node)

        if isinstance(node, ast.IfExp):
            test = self._eval(node.test)
            return _np().where(test, self._eval(node.body), self._eval(node.orelse))

        if isinstance(node, ast.Call):
            return self._call(node)

        raise ExprError(f"不允许的语法：{type(node).__name__}")

    def _binop(self, node: ast.BinOp) -> Any:
        left = self._eval(node.left)
        right = self._eval(node.right)
        op = node.op
        # 四则一律走 numpy：标量输入下 `1/0` 也要得到 inf 而不是 ZeroDivisionError
        np = _np()
        if isinstance(op, ast.Add):
            return np.add(left, right)
        if isinstance(op, ast.Sub):
            return np.subtract(left, right)
        if isinstance(op, ast.Mult):
            return np.multiply(left, right)
        if isinstance(op, ast.Div):
            return np.divide(left, right)
        if isinstance(op, (ast.FloorDiv, ast.Mod, ast.Pow)):
            if isinstance(op, ast.Pow) and isinstance(node.right, ast.Constant):
                exponent = float(node.right.value)
                if abs(exponent) > MAX_POW_EXPONENT:
                    raise ExprError(f"指数过大（上限 {MAX_POW_EXPONENT:g}）")
            if isinstance(op, ast.FloorDiv):
                return np.floor_divide(left, right)
            if isinstance(op, ast.Mod):
                return np.mod(left, right)
            return np.power(left, right)
        raise ExprError(f"不允许的运算符：{type(op).__name__}")

    def _compare(self, node: ast.Compare) -> Any:
        left = self._eval(node.left)
        result: Any = None
        for operator, comparator in zip(node.ops, node.comparators):
            right = self._eval(comparator)
            if isinstance(operator, ast.Eq):
                piece = left == right
            elif isinstance(operator, ast.NotEq):
                piece = left != right
            elif isinstance(operator, ast.Lt):
                piece = left < right
            elif isinstance(operator, ast.LtE):
                piece = left <= right
            elif isinstance(operator, ast.Gt):
                piece = left > right
            elif isinstance(operator, ast.GtE):
                piece = left >= right
            else:
                raise ExprError(f"不允许的比较运算：{type(operator).__name__}")
            result = piece if result is None else _np().logical_and(result, piece)
            left = right
        return result

    def _call(self, node: ast.Call) -> Any:
        if node.keywords:
            raise ExprError("函数调用不支持关键字参数")
        if not isinstance(node.func, ast.Name):
            raise ExprError("只允许调用白名单里的函数名")
        name = node.func.id
        entry = self.functions.get(name)
        if entry is None:
            raise ExprError(f"不允许的函数：{name}（可用：{'、'.join(allowed_functions())}）")
        arities, implementation = entry
        args = [self._eval(argument) for argument in node.args]
        if len(args) not in arities:
            expected = " / ".join(str(item) for item in arities)
            raise ExprError(f"{name} 需要 {expected} 个参数，收到 {len(args)} 个")
        return implementation(*args)


def _finalize(value: Any) -> Any:
    """把 numpy 标量收成 Python ``float``；数组原样返回。"""
    array = _np().asarray(value, dtype=float)
    if array.ndim == 0:
        return float(array)
    return array


def _check_variables(variables: Sequence[str]) -> tuple[str, ...]:
    names: list[str] = []
    for item in variables:
        name = str(item)
        if not name.isidentifier():
            raise ExprError(f"变量名不合法：{name}")
        if name in ALLOWED_CONSTANTS:
            raise ExprError(f"变量名不能与常量重名：{name}")
        if name not in names:
            names.append(name)
    return tuple(names)


@lru_cache(maxsize=256)
def _compiled(expr: str, variables: tuple[str, ...]) -> Any:
    tree = _parse(expr)
    functions = _functions()
    # 预检：拿假值跑一遍，把「不允许的名字 / 函数 / 参数个数」在编译期就报出来。
    # 数值上炸掉（例如 1/0）不算语法问题，真正的取值错误留到实际采样时暴露。
    try:
        with _np().errstate(all="ignore"):
            _Evaluator(dict.fromkeys(variables, 1.0), functions).run(tree.body)
    except ExprError:
        raise
    except Exception:  # noqa: BLE001 - 只做静态白名单检查，数值异常一律忽略
        pass

    def run(**values: Any) -> Any:
        missing = [name for name in variables if name not in values]
        if missing:
            raise ExprError("缺少变量：" + "、".join(missing))
        extra = [name for name in values if name not in variables]
        if extra:
            raise ExprError("多余的变量：" + "、".join(sorted(extra)))
        with _np().errstate(all="ignore"):
            return _finalize(_Evaluator(dict(values), functions).run(tree.body))

    return run


def compile_expr(expr: str, *, variables: Sequence[str] = DEFAULT_VARIABLES) -> Any:
    """编译成可调用对象 ``fn(**values)``；非法表达式抛 :class:`ExprError`。

    传入 ``float`` 返回 ``float``；传入 numpy ``ndarray`` 返回逐元素结果的 ``ndarray``。
    """
    names = _check_variables(variables)
    return _compiled(str(expr or "").strip(), names)


def evaluate(expr: str, /, **values: Any) -> Any:
    """便捷入口：编译 + 求值一步到位；非法表达式 / 缺变量抛 :class:`ExprError`。"""
    return compile_expr(expr, variables=tuple(values))(**values)


def check(expr: str, *, variables: Sequence[str] = DEFAULT_VARIABLES) -> list[str]:
    """校验表达式；返回问题清单，空列表表示通过。**永不抛异常**。"""
    try:
        compile_expr(expr, variables=variables)
    except ExprError as exc:
        return [str(exc)]
    except Exception as exc:  # noqa: BLE001 - 兜底：调用方只想要一份问题清单
        return [f"表达式无法编译：{exc}"]
    return []
