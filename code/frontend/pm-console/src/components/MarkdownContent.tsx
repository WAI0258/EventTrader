import type { ReactNode } from "react";

interface MarkdownContentProps {
  content: string;
  className?: string;
}

type MarkdownBlock =
  | { type: "heading"; level: number; text: string; key: string }
  | { type: "paragraph"; text: string; key: string }
  | { type: "list"; items: string[]; key: string }
  | { type: "code"; text: string; key: string }
  | { type: "rule"; key: string };

export function MarkdownContent({ content, className = "" }: MarkdownContentProps) {
  return (
    <div className={`reader-prose markdown-content ${className}`.trim()}>
      {parseMarkdownBlocks(content).map((block) => {
        switch (block.type) {
          case "heading":
            return block.level <= 2 ? (
              <h4 key={block.key} className="reader-heading-lg">
                {renderInlineMarkdown(block.text)}
              </h4>
            ) : (
              <h5 key={block.key} className="reader-heading-sm">
                {renderInlineMarkdown(block.text)}
              </h5>
            );
          case "list":
            return (
              <ul key={block.key} className="reader-list">
                {block.items.map((item, index) => (
                  <li key={`${block.key}-${index}`}>{renderInlineMarkdown(item)}</li>
                ))}
              </ul>
            );
          case "code":
            return (
              <pre key={block.key} className="reader-code-block">
                {block.text}
              </pre>
            );
          case "rule":
            return <hr key={block.key} className="reader-rule" />;
          default:
            return (
              <p key={block.key} className="reader-paragraph">
                {renderInlineMarkdown(block.text)}
              </p>
            );
        }
      })}
    </div>
  );
}

function parseMarkdownBlocks(content: string): MarkdownBlock[] {
  const lines = content.split("\n");
  const blocks: MarkdownBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const trimmed = lines[index].trim();
    if (!trimmed) {
      index += 1;
      continue;
    }

    if (trimmed === "---") {
      blocks.push({ type: "rule", key: `rule-${index}` });
      index += 1;
      continue;
    }

    if (trimmed.startsWith("```")) {
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        index += 1;
      }
      blocks.push({ type: "code", text: codeLines.join("\n"), key: `code-${index}` });
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length,
        text: heading[2],
        key: `heading-${index}`,
      });
      index += 1;
      continue;
    }

    if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      const items: string[] = [];
      while (index < lines.length) {
        const listLine = lines[index].trim();
        if (listLine.startsWith("- ") || listLine.startsWith("* ")) {
          items.push(listLine.slice(2).trim());
          index += 1;
          continue;
        }
        if (!listLine) {
          index += 1;
        }
        break;
      }
      blocks.push({ type: "list", items, key: `list-${index}` });
      continue;
    }

    const paragraphLines = [trimmed];
    index += 1;
    while (index < lines.length) {
      const next = lines[index].trim();
      if (
        !next ||
        next === "---" ||
        next.startsWith("```") ||
        /^(#{1,6})\s+/.test(next) ||
        next.startsWith("- ") ||
        next.startsWith("* ")
      ) {
        break;
      }
      paragraphLines.push(next);
      index += 1;
    }
    blocks.push({
      type: "paragraph",
      text: paragraphLines.join(" "),
      key: `paragraph-${index}`,
    });
  }

  return blocks;
}

function renderInlineMarkdown(text: string): ReactNode {
  const nodes: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let lastIndex = 0;

  for (const match of text.matchAll(pattern)) {
    const matchedText = match[0];
    const start = match.index ?? 0;
    if (start > lastIndex) {
      nodes.push(text.slice(lastIndex, start));
    }
    if (matchedText.startsWith("**") && matchedText.endsWith("**")) {
      nodes.push(<strong key={`${start}-strong`}>{matchedText.slice(2, -2)}</strong>);
    } else {
      nodes.push(<code key={`${start}-code`}>{matchedText.slice(1, -1)}</code>);
    }
    lastIndex = start + matchedText.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes.length ? nodes : text;
}
