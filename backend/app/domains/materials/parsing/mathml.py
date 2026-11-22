"""把常见 MathML 结构转换为 LaTeX，保留原文并拒绝未知结构。"""

from html import escape
import re
from xml.etree import ElementTree


class UnsupportedMathML(ValueError):
    pass


def mathml_to_latex(value: str) -> str:
    if len(value) > 100_000 or re.search(r"<!\s*(DOCTYPE|ENTITY)", value, re.I):
        raise UnsupportedMathML("MathML 包含不支持的声明或超出长度限制。")
    try:
        root = ElementTree.fromstring(value)
    except ElementTree.ParseError as exc:
        raise UnsupportedMathML("MathML 结构不完整。") from exc
    if sum(1 for _ in root.iter()) > 1000:
        raise UnsupportedMathML("MathML 节点过多。")

    def visit(node, depth=0):
        if depth > 40:
            raise UnsupportedMathML("MathML 嵌套过深。")
        tag = node.tag.rsplit("}", 1)[-1].lower()
        children = list(node)
        own = (node.text or "").strip()
        if tag in {"annotation", "annotation-xml"}:
            return ""
        if tag == "semantics":
            if not children:
                raise UnsupportedMathML("MathML 缺少表达式。")
            return visit(children[0], depth + 1)
        parts = [visit(child, depth + 1) for child in children]
        if tag in {"math", "mrow", "mstyle", "mpadded"}:
            return own + "".join(parts)
        if tag in {"mi", "mn", "mo", "mtext", "ms"}:
            # 文字中的反斜杠不执行；保留常见数学符号的原始语义。
            return own.replace("−", "-").replace("×", r"\times ").replace("÷", r"\div ")
        if tag == "mfrac" and len(parts) == 2:
            return rf"\frac{{{parts[0]}}}{{{parts[1]}}}"
        if tag == "msup" and len(parts) == 2:
            return f"{{{parts[0]}}}^{{{parts[1]}}}"
        if tag == "msub" and len(parts) == 2:
            return f"{{{parts[0]}}}_{{{parts[1]}}}"
        if tag == "msubsup" and len(parts) == 3:
            return f"{{{parts[0]}}}_{{{parts[1]}}}^{{{parts[2]}}}"
        if tag == "msqrt" and parts:
            return rf"\sqrt{{{''.join(parts)}}}"
        if tag == "mroot" and len(parts) == 2:
            return rf"\sqrt[{parts[1]}]{{{parts[0]}}}"
        if tag == "mfenced":
            opening, closing = node.get("open", "("), node.get("close", ")")
            separators = node.get("separators", ",") or ","
            return opening + separators[0].join(parts) + closing
        if tag == "mspace":
            return " "
        if tag in {"mtable", "mtr", "mtd", "mlabeledtr"}:
            # 行列边界不可直接删除，否则方程组或列式会被拼成一个表达式。
            raise UnsupportedMathML("表格型公式需要专门的布局适配器。")
        raise UnsupportedMathML(f"尚未支持的 MathML 元素：{tag}。")

    return visit(root).strip()


def serialize_math_node(node) -> str:
    """供 HTMLParser 的轻量节点树调用，不依赖浏览器或脚本执行。"""
    if isinstance(node, str):
        return escape(node)
    attrs = "".join(
        f' {escape(k)}="{escape(v, quote=True)}"' for k, v in node.attrs.items()
    )
    return (
        f"<{node.tag}{attrs}>"
        + "".join(serialize_math_node(c) for c in node.children)
        + f"</{node.tag}>"
    )
