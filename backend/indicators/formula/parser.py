"""
Pine Script 兼容解析器
实现完整的 Tokenizer（词法分析器）+ Parser（语法分析器）+ AST 节点定义。
支持 Pine Script 核心语法：变量声明、:= 赋值、var/varip、if-else、for/while、
三元运算符、ta.*/math.*/array.*/strategy.* 函数调用、input 函数等。
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any


class OpenScriptError(Exception):
    """解析错误"""
    pass


# ============================================================
# Token 定义
# ============================================================

class TokenType:
    # 字面量
    NUMBER = "NUMBER"
    STRING = "STRING"
    BOOL = "BOOL"
    NA = "NA"

    # 标识符与关键字
    IDENT = "IDENT"
    VAR = "VAR"
    VARIP = "VARIP"
    IF = "IF"
    ELSE = "ELSE"
    FOR = "FOR"
    WHILE = "WHILE"
    TO = "TO"
    BY = "BY"
    BREAK = "BREAK"
    CONTINUE = "CONTINUE"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    IMPORT = "IMPORT"

    # 运算符
    ASSIGN = "ASSIGN"          # =
    REASSIGN = "REASSIGN"      # :=
    PLUS = "PLUS"              # +
    MINUS = "MINUS"            # -
    STAR = "STAR"              # *
    SLASH = "SLASH"            # /
    PERCENT = "PERCENT"        # %
    EQ = "EQ"                  # ==
    NEQ = "NEQ"                # !=
    LT = "LT"                 # <
    GT = "GT"                  # >
    LTE = "LTE"               # <=
    GTE = "GTE"               # >=
    QUESTION = "QUESTION"      # ?
    COLON = "COLON"            # :
    COMMA = "COMMA"            # ,
    DOT = "DOT"                # .
    LPAREN = "LPAREN"          # (
    RPAREN = "RPAREN"          # )
    LBRACKET = "LBRACKET"      # [
    RBRACKET = "RBRACKET"      # ]
    ARROW = "ARROW"            # =>
    PLUS_ASSIGN = "PLUS_ASSIGN"    # +=
    MINUS_ASSIGN = "MINUS_ASSIGN"  # -=
    STAR_ASSIGN = "STAR_ASSIGN"    # *=
    SLASH_ASSIGN = "SLASH_ASSIGN"  # /=

    # 结构
    NEWLINE = "NEWLINE"
    INDENT = "INDENT"
    DEDENT = "DEDENT"
    EOF = "EOF"


@dataclass
class Token:
    type: str
    value: Any
    line: int
    col: int


KEYWORDS = {
    "var": TokenType.VAR,
    "varip": TokenType.VARIP,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "for": TokenType.FOR,
    "while": TokenType.WHILE,
    "to": TokenType.TO,
    "by": TokenType.BY,
    "break": TokenType.BREAK,
    "continue": TokenType.CONTINUE,
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
    "true": TokenType.BOOL,
    "false": TokenType.BOOL,
    "na": TokenType.NA,
    "import": TokenType.IMPORT,
}

# Pine Script 类型关键字（在解析时跳过这些类型注解）
PINE_TYPE_KEYWORDS = {
    "int", "float", "bool", "string", "color",
    "line", "label", "box", "table", "series",
}


# ============================================================
# Tokenizer（词法分析器）
# ============================================================

class Tokenizer:
    """将 Pine Script 源代码分解为 Token 流"""

    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []
        self.indent_stack: list[int] = [0]

    def tokenize(self) -> list[Token]:
        lines = self.source.split("\n")
        for line_num, line_text in enumerate(lines, 1):
            self.line = line_num
            self._tokenize_line(line_text, line_num)

        # 生成剩余 DEDENT
        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            self.tokens.append(Token(TokenType.DEDENT, "", self.line, 0))

        self.tokens.append(Token(TokenType.EOF, "", self.line, 0))
        return self.tokens

    def _tokenize_line(self, line: str, line_num: int):
        # 移除注释
        line = self._strip_comment(line)

        # 空行跳过
        stripped = line.strip()
        if not stripped:
            return

        # 计算缩进
        indent = 0
        for ch in line:
            if ch == ' ':
                indent += 1
            elif ch == '\t':
                indent += 4
            else:
                break

        # 生成 INDENT/DEDENT
        current_indent = self.indent_stack[-1]
        if indent > current_indent:
            self.indent_stack.append(indent)
            self.tokens.append(Token(TokenType.INDENT, indent, line_num, 0))
        elif indent < current_indent:
            while len(self.indent_stack) > 1 and self.indent_stack[-1] > indent:
                self.indent_stack.pop()
                self.tokens.append(Token(TokenType.DEDENT, "", line_num, 0))

        # 词法分析行内 Token
        pos = indent
        while pos < len(line):
            ch = line[pos]

            # 空白跳过
            if ch in (' ', '\t'):
                pos += 1
                continue

            col = pos + 1

            # 字符串
            if ch in ('"', "'"):
                s, pos = self._read_string(line, pos)
                self.tokens.append(Token(TokenType.STRING, s, line_num, col))
                continue

            # 数字
            if ch.isdigit() or (ch == '.' and pos + 1 < len(line) and line[pos + 1].isdigit()):
                num, pos = self._read_number(line, pos)
                self.tokens.append(Token(TokenType.NUMBER, num, line_num, col))
                continue

            # 标识符/关键字
            if ch.isalpha() or ch == '_':
                word, pos = self._read_ident(line, pos)
                kw = KEYWORDS.get(word)
                if kw:
                    if kw == TokenType.BOOL:
                        self.tokens.append(Token(TokenType.BOOL, word == "true", line_num, col))
                    else:
                        self.tokens.append(Token(kw, word, line_num, col))
                else:
                    self.tokens.append(Token(TokenType.IDENT, word, line_num, col))
                continue

            # 双字符运算符
            two = line[pos:pos + 2]
            if two == ":=":
                self.tokens.append(Token(TokenType.REASSIGN, ":=", line_num, col))
                pos += 2
                continue
            if two == "==":
                self.tokens.append(Token(TokenType.EQ, "==", line_num, col))
                pos += 2
                continue
            if two == "!=":
                self.tokens.append(Token(TokenType.NEQ, "!=", line_num, col))
                pos += 2
                continue
            if two == "<=":
                self.tokens.append(Token(TokenType.LTE, "<=", line_num, col))
                pos += 2
                continue
            if two == ">=":
                self.tokens.append(Token(TokenType.GTE, ">=", line_num, col))
                pos += 2
                continue
            if two == "=>":
                self.tokens.append(Token(TokenType.ARROW, "=>", line_num, col))
                pos += 2
                continue
            if two == "+=":
                self.tokens.append(Token(TokenType.PLUS_ASSIGN, "+=", line_num, col))
                pos += 2
                continue
            if two == "-=":
                self.tokens.append(Token(TokenType.MINUS_ASSIGN, "-=", line_num, col))
                pos += 2
                continue
            if two == "*=":
                self.tokens.append(Token(TokenType.STAR_ASSIGN, "*=", line_num, col))
                pos += 2
                continue
            if two == "/=":
                self.tokens.append(Token(TokenType.SLASH_ASSIGN, "/=", line_num, col))
                pos += 2
                continue

            # 单字符运算符
            single_map = {
                '=': TokenType.ASSIGN, '+': TokenType.PLUS, '-': TokenType.MINUS,
                '*': TokenType.STAR, '/': TokenType.SLASH, '%': TokenType.PERCENT,
                '<': TokenType.LT, '>': TokenType.GT,
                '?': TokenType.QUESTION, ':': TokenType.COLON,
                ',': TokenType.COMMA, '.': TokenType.DOT,
                '(': TokenType.LPAREN, ')': TokenType.RPAREN,
                '[': TokenType.LBRACKET, ']': TokenType.RBRACKET,
            }
            if ch in single_map:
                self.tokens.append(Token(single_map[ch], ch, line_num, col))
                pos += 1
                continue

            # 未知字符，跳过
            pos += 1

        # 行末换行
        self.tokens.append(Token(TokenType.NEWLINE, "\\n", line_num, len(line)))

    def _strip_comment(self, line: str) -> str:
        in_string = False
        quote_char = None
        i = 0
        while i < len(line):
            ch = line[i]
            if in_string:
                if ch == quote_char:
                    in_string = False
            else:
                if ch in ('"', "'"):
                    in_string = True
                    quote_char = ch
                elif ch == '/' and i + 1 < len(line) and line[i + 1] == '/':
                    return line[:i]
            i += 1
        return line

    def _read_string(self, line: str, pos: int) -> tuple[str, int]:
        quote = line[pos]
        pos += 1
        result = []
        while pos < len(line):
            ch = line[pos]
            if ch == '\\' and pos + 1 < len(line):
                next_ch = line[pos + 1]
                escape_map = {'n': '\n', 't': '\t', '\\': '\\', '"': '"', "'": "'"}
                result.append(escape_map.get(next_ch, next_ch))
                pos += 2
                continue
            if ch == quote:
                pos += 1
                return ''.join(result), pos
            result.append(ch)
            pos += 1
        return ''.join(result), pos

    def _read_number(self, line: str, pos: int) -> tuple[float | int, int]:
        start = pos
        has_dot = False
        has_exp = False
        while pos < len(line) and (line[pos].isdigit() or line[pos] == '.'):
            if line[pos] == '.':
                if pos + 1 < len(line) and line[pos + 1].isdigit():
                    has_dot = True
                else:
                    break
            pos += 1
        # 科学计数法: 1e18, 1e-7, 2.5e10
        # 只有e后面紧跟数字或+/-数字时才识别为科学计数法
        if pos < len(line) and line[pos] in ('e', 'E'):
            next_pos = pos + 1
            if next_pos < len(line) and line[next_pos] in ('+', '-'):
                next_pos += 1
            if next_pos < len(line) and line[next_pos].isdigit():
                # 确认是科学计数法
                has_exp = True
                pos += 1  # skip 'e'
                if pos < len(line) and line[pos] in ('+', '-'):
                    pos += 1
                while pos < len(line) and line[pos].isdigit():
                    pos += 1
        text = line[start:pos]
        if has_dot or has_exp:
            return float(text), pos
        return int(text), pos

    def _read_ident(self, line: str, pos: int) -> tuple[str, int]:
        start = pos
        while pos < len(line) and (line[pos].isalnum() or line[pos] == '_'):
            pos += 1
        return line[start:pos], pos


# ============================================================
# AST 节点定义
# ============================================================

@dataclass
class ASTNode:
    line: int = 0


@dataclass
class NumberLiteral(ASTNode):
    value: float | int = 0


@dataclass
class StringLiteral(ASTNode):
    value: str = ""


@dataclass
class BoolLiteral(ASTNode):
    value: bool = False


@dataclass
class NALiteral(ASTNode):
    pass


@dataclass
class Identifier(ASTNode):
    name: str = ""


@dataclass
class DotAccess(ASTNode):
    """点访问：obj.attr"""
    obj: ASTNode = None
    attr: str = ""


@dataclass
class IndexAccess(ASTNode):
    """索引访问：arr[index]"""
    obj: ASTNode = None
    index: ASTNode = None


@dataclass
class BinaryOp(ASTNode):
    op: str = ""
    left: ASTNode = None
    right: ASTNode = None


@dataclass
class UnaryOp(ASTNode):
    op: str = ""
    operand: ASTNode = None


@dataclass
class TernaryOp(ASTNode):
    condition: ASTNode = None
    true_expr: ASTNode = None
    false_expr: ASTNode = None


@dataclass
class FunctionCall(ASTNode):
    func: ASTNode = None
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)


@dataclass
class Assignment(ASTNode):
    """首次声明赋值 x = expr"""
    name: str = ""
    value: ASTNode = None
    is_var: bool = False      # var 声明
    is_varip: bool = False    # varip 声明


@dataclass
class Reassignment(ASTNode):
    """重新赋值 x := expr"""
    name: str = ""
    value: ASTNode = None


@dataclass
class CompoundAssignment(ASTNode):
    """复合赋值 x += expr, x -= expr 等"""
    name: str = ""
    op: str = ""  # "+", "-", "*", "/"
    value: ASTNode = None


@dataclass
class IfBlock(ASTNode):
    condition: ASTNode = None
    body: list = field(default_factory=list)
    elif_blocks: list = field(default_factory=list)  # [(condition, body), ...]
    else_body: list = field(default_factory=list)


@dataclass
class ForBlock(ASTNode):
    var_name: str = ""
    start: ASTNode = None
    end: ASTNode = None
    step: ASTNode = None
    body: list = field(default_factory=list)


@dataclass
class WhileBlock(ASTNode):
    condition: ASTNode = None
    body: list = field(default_factory=list)


@dataclass
class BreakStmt(ASTNode):
    pass


@dataclass
class ContinueStmt(ASTNode):
    pass


@dataclass
class ExprStatement(ASTNode):
    """表达式语句（如函数调用）"""
    expr: ASTNode = None


@dataclass
class TupleDestructure(ASTNode):
    """多重赋值 [a, b, c] = func()"""
    names: list = field(default_factory=list)
    value: ASTNode = None


@dataclass
class FunctionDef(ASTNode):
    """用户定义函数 f(params) => body"""
    name: str = ""
    params: list = field(default_factory=list)
    body: list = field(default_factory=list)


@dataclass
class Program(ASTNode):
    statements: list = field(default_factory=list)


# ============================================================
# Parser（语法分析器）
# ============================================================

class Parser:
    """递归下降 Pine Script 解析器"""

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> Program:
        stmts = []
        self._skip_newlines()
        while not self._at_end():
            stmt = self._parse_statement()
            if stmt is not None:
                stmts.append(stmt)
            self._skip_newlines()
        return Program(statements=stmts)

    # --- 工具方法 ---

    def _current(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return Token(TokenType.EOF, "", 0, 0)

    def _peek(self, offset: int = 0) -> Token:
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return Token(TokenType.EOF, "", 0, 0)

    def _at_end(self) -> bool:
        return self._current().type == TokenType.EOF

    def _advance(self) -> Token:
        tok = self._current()
        self.pos += 1
        return tok

    def _expect(self, tok_type: str) -> Token:
        tok = self._current()
        if tok.type != tok_type:
            raise OpenScriptError(
                f"第 {tok.line} 行: 期望 {tok_type}，得到 {tok.type} ({tok.value!r})")
        return self._advance()

    def _match(self, *types) -> Token | None:
        if self._current().type in types:
            return self._advance()
        return None

    def _skip_newlines(self):
        while self._current().type == TokenType.NEWLINE:
            self._advance()

    # --- 辅助：跳过类型注解 ---

    def _skip_type_annotation(self):
        """跳过 Pine Script 类型注解（如 float, int[], string 等）"""
        if (self._current().type == TokenType.IDENT
                and self._current().value in PINE_TYPE_KEYWORDS):
            self._advance()  # 跳过类型名
            # 跳过可选的 [] （数组类型）
            if (self._current().type == TokenType.LBRACKET
                    and self._peek(1).type == TokenType.RBRACKET):
                self._advance()  # [
                self._advance()  # ]

    # --- 语句解析 ---

    def _parse_statement(self) -> ASTNode | None:
        tok = self._current()

        if tok.type == TokenType.IMPORT:
            raise OpenScriptError(f"第 {tok.line} 行: 禁止使用 import 语句")

        if tok.type == TokenType.IF:
            return self._parse_if()

        if tok.type == TokenType.FOR:
            return self._parse_for()

        if tok.type == TokenType.WHILE:
            return self._parse_while()

        if tok.type == TokenType.BREAK:
            self._advance()
            return BreakStmt(line=tok.line)

        if tok.type == TokenType.CONTINUE:
            self._advance()
            return ContinueStmt(line=tok.line)

        if tok.type in (TokenType.VAR, TokenType.VARIP):
            return self._parse_var_decl()

        # 检查 [a, b] = ... 解构赋值（需要向前看确认是解构而非数组字面量）
        if tok.type == TokenType.LBRACKET and self._is_destructure_assign():
            return self._parse_tuple_destructure()

        # 类型注解开头的变量声明（如 int DIR_UP = 1, float x = 3.14）
        if (tok.type == TokenType.IDENT
                and tok.value in PINE_TYPE_KEYWORDS
                and self._is_type_annotated_decl()):
            return self._parse_typed_decl()

        # 标识符开头：赋值、函数定义或表达式
        if tok.type == TokenType.IDENT:
            # 检查是否是函数定义: name(params) =>
            if self._is_function_def():
                return self._parse_function_def()
            return self._parse_ident_statement()

        # 其他表达式语句
        expr = self._parse_expression()
        return ExprStatement(expr=expr, line=expr.line)

    def _is_destructure_assign(self) -> bool:
        """向前看判断 [a, b, c] = ... 是否是解构赋值"""
        save_pos = self.pos
        try:
            self._advance()  # 跳过 [
            # 跳过到匹配的 ]
            depth = 1
            while depth > 0 and not self._at_end():
                if self._current().type == TokenType.LBRACKET:
                    depth += 1
                elif self._current().type == TokenType.RBRACKET:
                    depth -= 1
                self._advance()
            # ] 后面应该是 =
            return self._current().type == TokenType.ASSIGN
        finally:
            self.pos = save_pos

    def _is_type_annotated_decl(self) -> bool:
        """向前看判断是否是类型注解声明（如 int x = 1）"""
        save_pos = self.pos
        self._advance()  # 跳过类型名
        # 跳过可选的 []
        if (self._current().type == TokenType.LBRACKET
                and self._peek(1).type == TokenType.RBRACKET):
            self._advance()
            self._advance()
        # 下一个应该是标识符
        result = (self._current().type == TokenType.IDENT
                  and self._peek(1).type == TokenType.ASSIGN)
        self.pos = save_pos
        return result

    def _parse_typed_decl(self) -> Assignment:
        """解析带类型注解的变量声明"""
        tok = self._current()
        self._skip_type_annotation()
        name_tok = self._expect(TokenType.IDENT)
        self._expect(TokenType.ASSIGN)
        value = self._parse_expression()
        return Assignment(name=name_tok.value, value=value, line=tok.line)

    def _is_function_def(self) -> bool:
        """向前看判断是否是函数定义: name(params) =>"""
        save_pos = self.pos
        try:
            if self._current().type != TokenType.IDENT:
                return False
            self._advance()  # name
            if self._current().type != TokenType.LPAREN:
                return False
            self._advance()  # (
            # 跳过参数直到 )
            depth = 1
            while depth > 0 and not self._at_end():
                if self._current().type == TokenType.LPAREN:
                    depth += 1
                elif self._current().type == TokenType.RPAREN:
                    depth -= 1
                self._advance()
            # 下一个应该是 =>
            return self._current().type == TokenType.ARROW
        finally:
            self.pos = save_pos

    def _parse_function_def(self) -> FunctionDef:
        """解析函数定义 name(params) => body"""
        tok = self._current()
        name_tok = self._advance()  # 函数名
        self._expect(TokenType.LPAREN)

        # 解析参数
        params = []
        while self._current().type != TokenType.RPAREN:
            param_tok = self._expect(TokenType.IDENT)
            params.append(param_tok.value)
            if self._current().type == TokenType.COMMA:
                self._advance()
        self._expect(TokenType.RPAREN)
        self._expect(TokenType.ARROW)  # =>

        # 解析函数体（可以是缩进块或单行表达式）
        body = self._parse_block()

        return FunctionDef(name=name_tok.value, params=params, body=body, line=tok.line)

    def _parse_var_decl(self) -> Assignment:
        tok = self._advance()  # var 或 varip
        is_var = tok.type == TokenType.VAR
        is_varip = tok.type == TokenType.VARIP

        # 跳过类型注解（如 var float[] arr = ...，var int x = ...）
        self._skip_type_annotation()

        name_tok = self._expect(TokenType.IDENT)
        self._expect(TokenType.ASSIGN)
        value = self._parse_expression()

        return Assignment(
            name=name_tok.value, value=value,
            is_var=is_var, is_varip=is_varip,
            line=tok.line
        )

    def _parse_ident_statement(self) -> ASTNode:
        """解析标识符开头的语句：赋值、重新赋值、函数调用等"""
        # 收集完整标识符（可能包含点访问如 ta.sma, strategy.entry）
        # 先看是否是简单赋值 name = expr 或 name := expr
        name_tok = self._current()

        # 向前看判断是否是赋值
        # 需要跳过可能的点访问来找到赋值符号
        save_pos = self.pos
        self._advance()  # 跳过第一个 IDENT

        # 简单赋值: name = expr
        if self._current().type == TokenType.ASSIGN:
            self._advance()
            value = self._parse_expression()
            return Assignment(name=name_tok.value, value=value, line=name_tok.line)

        # 重新赋值: name := expr
        if self._current().type == TokenType.REASSIGN:
            self._advance()
            value = self._parse_expression()
            return Reassignment(name=name_tok.value, value=value, line=name_tok.line)

        # 复合赋值: name += expr, name -= expr 等
        if self._current().type in (TokenType.PLUS_ASSIGN, TokenType.MINUS_ASSIGN,
                                     TokenType.STAR_ASSIGN, TokenType.SLASH_ASSIGN):
            op_tok = self._advance()
            op_map = {"+=": "+", "-=": "-", "*=": "*", "/=": "/"}
            value = self._parse_expression()
            return CompoundAssignment(
                name=name_tok.value, op=op_map[op_tok.value],
                value=value, line=name_tok.line
            )

        # 不是赋值，回退并解析为表达式
        self.pos = save_pos
        expr = self._parse_expression()
        return ExprStatement(expr=expr, line=expr.line)

    def _parse_tuple_destructure(self) -> TupleDestructure:
        tok = self._advance()  # [
        names = []
        while self._current().type != TokenType.RBRACKET:
            name_tok = self._expect(TokenType.IDENT)
            names.append(name_tok.value)
            if self._current().type == TokenType.COMMA:
                self._advance()
        self._expect(TokenType.RBRACKET)
        self._expect(TokenType.ASSIGN)
        value = self._parse_expression()
        return TupleDestructure(names=names, value=value, line=tok.line)

    def _parse_if(self) -> IfBlock:
        tok = self._advance()  # if
        condition = self._parse_expression()
        body = self._parse_block()

        elif_blocks = []
        else_body = []

        while True:
            self._skip_newlines()
            if (self._current().type == TokenType.ELSE
                    and self._peek(1).type == TokenType.IF):
                self._advance()  # else
                self._advance()  # if
                elif_cond = self._parse_expression()
                elif_body = self._parse_block()
                elif_blocks.append((elif_cond, elif_body))
            elif self._current().type == TokenType.ELSE:
                self._advance()  # else
                else_body = self._parse_block()
                break
            else:
                break

        return IfBlock(
            condition=condition, body=body,
            elif_blocks=elif_blocks, else_body=else_body,
            line=tok.line
        )

    def _parse_for(self) -> ForBlock:
        tok = self._advance()  # for
        var_tok = self._expect(TokenType.IDENT)
        self._expect(TokenType.ASSIGN)
        start = self._parse_expression()
        self._expect(TokenType.TO)
        end = self._parse_expression()

        step = None
        if self._current().type == TokenType.BY:
            self._advance()
            step = self._parse_expression()

        body = self._parse_block()

        return ForBlock(
            var_name=var_tok.value, start=start, end=end, step=step,
            body=body, line=tok.line
        )

    def _parse_while(self) -> WhileBlock:
        tok = self._advance()  # while
        condition = self._parse_expression()
        body = self._parse_block()
        return WhileBlock(condition=condition, body=body, line=tok.line)

    def _parse_block(self) -> list[ASTNode]:
        """解析缩进块"""
        stmts = []
        self._skip_newlines()

        if self._current().type == TokenType.INDENT:
            self._advance()  # 消费 INDENT
            self._skip_newlines()
            while self._current().type not in (TokenType.DEDENT, TokenType.EOF):
                stmt = self._parse_statement()
                if stmt is not None:
                    stmts.append(stmt)
                self._skip_newlines()
            if self._current().type == TokenType.DEDENT:
                self._advance()
        else:
            # 单行块（同一行）
            # 如果当前行还有表达式，解析它
            if self._current().type not in (TokenType.NEWLINE, TokenType.EOF):
                stmt = self._parse_statement()
                if stmt is not None:
                    stmts.append(stmt)

        return stmts

    # --- 表达式解析（优先级递归下降） ---

    def _parse_expression(self) -> ASTNode:
        return self._parse_ternary()

    def _parse_ternary(self) -> ASTNode:
        expr = self._parse_or()
        if self._current().type == TokenType.QUESTION:
            self._advance()
            true_expr = self._parse_expression()
            self._expect(TokenType.COLON)
            false_expr = self._parse_expression()
            return TernaryOp(
                condition=expr, true_expr=true_expr,
                false_expr=false_expr, line=expr.line
            )
        return expr

    def _parse_or(self) -> ASTNode:
        left = self._parse_and()
        while self._current().type == TokenType.OR:
            self._advance()
            right = self._parse_and()
            left = BinaryOp(op="or", left=left, right=right, line=left.line)
        return left

    def _parse_and(self) -> ASTNode:
        left = self._parse_not()
        while self._current().type == TokenType.AND:
            self._advance()
            right = self._parse_not()
            left = BinaryOp(op="and", left=left, right=right, line=left.line)
        return left

    def _parse_not(self) -> ASTNode:
        if self._current().type == TokenType.NOT:
            tok = self._advance()
            operand = self._parse_not()
            return UnaryOp(op="not", operand=operand, line=tok.line)
        return self._parse_comparison()

    def _parse_comparison(self) -> ASTNode:
        left = self._parse_addition()
        comp_ops = {TokenType.EQ: "==", TokenType.NEQ: "!=",
                    TokenType.LT: "<", TokenType.GT: ">",
                    TokenType.LTE: "<=", TokenType.GTE: ">="}
        while self._current().type in comp_ops:
            op = comp_ops[self._current().type]
            self._advance()
            right = self._parse_addition()
            left = BinaryOp(op=op, left=left, right=right, line=left.line)
        return left

    def _parse_addition(self) -> ASTNode:
        left = self._parse_multiplication()
        while self._current().type in (TokenType.PLUS, TokenType.MINUS):
            op = "+" if self._current().type == TokenType.PLUS else "-"
            self._advance()
            right = self._parse_multiplication()
            left = BinaryOp(op=op, left=left, right=right, line=left.line)
        return left

    def _parse_multiplication(self) -> ASTNode:
        left = self._parse_unary()
        while self._current().type in (TokenType.STAR, TokenType.SLASH, TokenType.PERCENT):
            op_map = {TokenType.STAR: "*", TokenType.SLASH: "/", TokenType.PERCENT: "%"}
            op = op_map[self._current().type]
            self._advance()
            right = self._parse_unary()
            left = BinaryOp(op=op, left=left, right=right, line=left.line)
        return left

    def _parse_unary(self) -> ASTNode:
        if self._current().type == TokenType.MINUS:
            tok = self._advance()
            operand = self._parse_unary()
            return UnaryOp(op="-", operand=operand, line=tok.line)
        if self._current().type == TokenType.PLUS:
            self._advance()
            return self._parse_unary()
        return self._parse_postfix()

    def _parse_postfix(self) -> ASTNode:
        expr = self._parse_primary()

        while True:
            if self._current().type == TokenType.DOT:
                self._advance()
                attr_tok = self._expect(TokenType.IDENT)
                expr = DotAccess(obj=expr, attr=attr_tok.value, line=expr.line)
            elif self._current().type == TokenType.LBRACKET:
                self._advance()
                index = self._parse_expression()
                self._expect(TokenType.RBRACKET)
                expr = IndexAccess(obj=expr, index=index, line=expr.line)
            elif self._current().type == TokenType.LPAREN:
                expr = self._parse_call(expr)
            else:
                break

        return expr

    def _parse_call(self, func: ASTNode) -> FunctionCall:
        self._advance()  # (
        args = []
        kwargs = {}

        while self._current().type != TokenType.RPAREN:
            if self._at_end():
                raise OpenScriptError("未关闭的函数调用括号")

            # 检查是否是 keyword=value
            if (self._current().type == TokenType.IDENT
                    and self._peek(1).type == TokenType.ASSIGN):
                key_tok = self._advance()
                self._advance()  # =
                value = self._parse_expression()
                kwargs[key_tok.value] = value
            else:
                arg = self._parse_expression()
                args.append(arg)

            if self._current().type == TokenType.COMMA:
                self._advance()

        self._expect(TokenType.RPAREN)
        return FunctionCall(func=func, args=args, kwargs=kwargs, line=func.line)

    def _parse_primary(self) -> ASTNode:
        tok = self._current()

        if tok.type == TokenType.NUMBER:
            self._advance()
            return NumberLiteral(value=tok.value, line=tok.line)

        if tok.type == TokenType.STRING:
            self._advance()
            return StringLiteral(value=tok.value, line=tok.line)

        if tok.type == TokenType.BOOL:
            self._advance()
            return BoolLiteral(value=tok.value, line=tok.line)

        if tok.type == TokenType.NA:
            self._advance()
            return NALiteral(line=tok.line)

        if tok.type == TokenType.IDENT:
            self._advance()
            return Identifier(name=tok.value, line=tok.line)

        if tok.type == TokenType.LPAREN:
            self._advance()
            expr = self._parse_expression()
            self._expect(TokenType.RPAREN)
            return expr

        if tok.type == TokenType.LBRACKET:
            # 数组字面量 [a, b, c]
            self._advance()
            elements = []
            while self._current().type != TokenType.RBRACKET:
                elements.append(self._parse_expression())
                if self._current().type == TokenType.COMMA:
                    self._advance()
            self._expect(TokenType.RBRACKET)
            return FunctionCall(
                func=Identifier(name="__array_literal__", line=tok.line),
                args=elements, kwargs={}, line=tok.line
            )

        if tok.type == TokenType.IF:
            # 内联 if 表达式
            return self._parse_if()

        raise OpenScriptError(
            f"第 {tok.line} 行: 意外的 Token {tok.type} ({tok.value!r})")


# ============================================================
# 解析结果 (保持向后兼容)
# ============================================================

class InputParam:
    """用户输入参数"""
    def __init__(self, name: str, default: Any, title: str = "",
                 param_type: str = "int", min_val: Any = None, max_val: Any = None):
        self.name = name
        self.default = default
        self.title = title or name
        self.param_type = param_type
        self.min_val = min_val
        self.max_val = max_val

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "default": self.default,
            "title": self.title,
            "type": self.param_type,
            "min": self.min_val,
            "max": self.max_val,
        }


class ParseResult:
    """解析结果"""
    def __init__(self):
        self.mode = "indicator"
        self.name = ""
        self.overlay = False
        self.initial_capital = 100000
        self.inputs: list[InputParam] = []
        self.ast: Program = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "name": self.name,
            "overlay": self.overlay,
            "initial_capital": self.initial_capital,
            "inputs": [inp.to_dict() for inp in self.inputs],
        }


# ============================================================
# 公开接口
# ============================================================

def parse_openscript(code: str, user_params: dict[str, Any] | None = None) -> ParseResult:
    """
    解析 Pine Script / OpenScript 代码，返回 AST 和元信息。

    Args:
        code: 源代码
        user_params: 用户自定义参数（未使用，保留兼容）

    Returns:
        ParseResult 含 AST 和元信息
    """
    result = ParseResult()

    # 词法分析
    tokenizer = Tokenizer(code)
    tokens = tokenizer.tokenize()

    # 语法分析
    parser = Parser(tokens)
    program = parser.parse()
    result.ast = program

    # 遍历 AST 提取元信息
    _extract_meta(program, result)

    return result


def validate_openscript(code: str) -> list[str]:
    """验证代码，返回错误列表"""
    errors = []
    try:
        parse_openscript(code)
    except OpenScriptError as e:
        errors.append(str(e))
    except Exception as e:
        errors.append(f"未知错误: {e}")
    return errors


def _extract_meta(program: Program, result: ParseResult):
    """从 AST 中提取 indicator/strategy/input 声明信息"""
    for stmt in program.statements:
        if isinstance(stmt, ExprStatement) and isinstance(stmt.expr, FunctionCall):
            call = stmt.expr
            func_name = _get_func_name(call.func)

            if func_name == "indicator":
                result.mode = "indicator"
                if call.args:
                    if isinstance(call.args[0], StringLiteral):
                        result.name = call.args[0].value
                if "overlay" in call.kwargs:
                    v = call.kwargs["overlay"]
                    if isinstance(v, BoolLiteral):
                        result.overlay = v.value

            elif func_name == "strategy":
                result.mode = "strategy"
                if call.args:
                    if isinstance(call.args[0], StringLiteral):
                        result.name = call.args[0].value
                if "overlay" in call.kwargs:
                    v = call.kwargs["overlay"]
                    if isinstance(v, BoolLiteral):
                        result.overlay = v.value
                if "initial_capital" in call.kwargs:
                    v = call.kwargs["initial_capital"]
                    if isinstance(v, NumberLiteral):
                        result.initial_capital = v.value

        elif isinstance(stmt, Assignment):
            # 提取 input 参数
            if isinstance(stmt.value, FunctionCall):
                func_name = _get_func_name(stmt.value.func)
                if func_name in ("input", "input.int", "input.float",
                                  "input.bool", "input.string", "input.source"):
                    _extract_input(stmt.name, stmt.value, result)


def _extract_input(name: str, call: FunctionCall, result: ParseResult):
    """从 input() 调用中提取参数信息"""
    default = None
    title = name
    param_type = "int"

    func_name = _get_func_name(call.func)

    # input.bool / input.float / input.source 等
    type_map = {
        "input.bool": "bool",
        "input.float": "float",
        "input.int": "int",
        "input.string": "string",
        "input.source": "source",
    }
    if func_name in type_map:
        param_type = type_map[func_name]

    if call.args:
        arg0 = call.args[0]
        if isinstance(arg0, NumberLiteral):
            default = arg0.value
        elif isinstance(arg0, StringLiteral):
            default = arg0.value
        elif isinstance(arg0, BoolLiteral):
            default = arg0.value
            param_type = "bool"
        elif isinstance(arg0, Identifier):
            default = arg0.name  # 如 close

    if "title" in call.kwargs:
        v = call.kwargs["title"]
        if isinstance(v, StringLiteral):
            title = v.value

    if "defval" in call.kwargs:
        v = call.kwargs["defval"]
        if isinstance(v, NumberLiteral):
            default = v.value
        elif isinstance(v, StringLiteral):
            default = v.value
        elif isinstance(v, BoolLiteral):
            default = v.value

    if isinstance(default, float):
        param_type = "float"
    elif isinstance(default, bool):
        param_type = "bool"

    result.inputs.append(InputParam(
        name=name, default=default, title=title, param_type=param_type
    ))


def _get_func_name(node: ASTNode) -> str:
    """获取函数调用的完整名称（处理点访问）"""
    if isinstance(node, Identifier):
        return node.name
    if isinstance(node, DotAccess):
        parent = _get_func_name(node.obj)
        return f"{parent}.{node.attr}"
    return ""
