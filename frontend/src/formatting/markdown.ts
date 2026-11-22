// 保留代码示例中的分隔符；未结束的代码块在流式生成期间也保持原样。
export function normalizeMathDelimiters(content: string): string {
  return content.replace(
    /(`{3,}|~{3,})[\s\S]*?(?:\1|$)|(`+)[^\n]*?\2|\\\[([\s\S]*?)\\\]|\\\(([\s\S]*?)\\\)/g,
    (original, fence, inlineCode, blockMath, inlineMath) => {
      if (fence || inlineCode) return original;
      // remark-math 需要独占一行的 $$ 才会按块公式渲染。
      if (blockMath !== undefined) return `\n$$\n${blockMath.trim()}\n$$\n`;
      return `$${inlineMath}$`;
    },
  );
}
