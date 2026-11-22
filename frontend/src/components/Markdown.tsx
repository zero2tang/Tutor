import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import remarkGfm from "remark-gfm";
import rehypeKatex from "rehype-katex";
import { normalizeMathDelimiters } from "../formatting/markdown";
import "katex/dist/katex.min.css";

export function Markdown({ children }: { children: string }) {
  // 同时接受模型常见的 LaTeX 行内和块级分隔符。
  const content = normalizeMathDelimiters(children);
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkMath, remarkGfm]}
        rehypePlugins={[rehypeKatex]}
        components={{
          img: ({ src, alt }) => {
            // 数据集配图仅走服务端的本地白名单资源，不自动请求外部图片。
            if (
              !src ||
              !/^\/api\/assets\/math\/[A-Za-z0-9_.-]+\.(?:png|jpg|jpeg|webp|gif)$/i.test(
                src,
              )
            )
              return (
                <span className="image-unavailable">配图资源暂不可用</span>
              );
            return (
              <a href={src} target="_blank" rel="noreferrer" title="查看原图">
                <img
                  className="lesson-image"
                  src={src}
                  alt={alt || "题目配图"}
                  loading="lazy"
                  decoding="async"
                />
              </a>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
