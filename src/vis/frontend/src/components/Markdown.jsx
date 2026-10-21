// Minimal Markdown renderer for agent/operator message bubbles.
// Builds React elements only (no dangerouslySetInnerHTML), so agent text
// cannot inject markup. Supports headings, bold/italic, inline code,
// fenced code blocks, bullet/numbered lists, and blockquotes.
import React from "react";

const INLINE_RULES = [
  { pattern: /\*\*([^*]+)\*\*/, wrap: (children, key) => <strong key={key}>{children}</strong> },
  { pattern: /\*([^*\n]+)\*/, wrap: (children, key) => <em key={key}>{children}</em> },
  { pattern: /`([^`\n]+)`/, wrap: (children, key) => <code key={key}>{children}</code> },
];

function renderInline(text, keyPrefix = "i") {
  const parts = [text];
  for (const rule of INLINE_RULES) {
    for (let i = 0; i < parts.length; i += 1) {
      const part = parts[i];
      if (typeof part !== "string") continue;
      const match = part.match(rule.pattern);
      if (!match) continue;
      const before = part.slice(0, match.index);
      const after = part.slice(match.index + match[0].length);
      const inner = renderInline(match[1], `${keyPrefix}-${i}`);
      parts.splice(i, 1, before, rule.wrap(inner, `${keyPrefix}-${i}`), after);
      i += 1;
    }
  }
  return parts.filter((part) => part !== "");
}

const HEADING_RE = /^(#{1,4})\s+(.*)$/;
const FENCE_RE = /^```(.*)$/;
const BULLET_RE = /^\s*[-*•]\s+(.*)$/;
const NUMBER_RE = /^\s*(\d+)[.)]\s+(.*)$/;
const QUOTE_RE = /^>\s?(.*)$/;

export default function Markdown({ text }) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let paragraph = [];
  let list = null;
  let quote = [];
  let fence = null;
  let blockKey = 0;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const body = paragraph.join(" ").trim();
    paragraph = [];
    if (!body) return;
    blocks.push(<p key={`p${blockKey++}`}>{renderInline(body)}</p>);
  };
  const flushList = () => {
    if (!list) return;
    const items = list.items.map((item, i) => <li key={i}>{renderInline(item)}</li>);
    blocks.push(
      list.ordered
        ? <ol key={`l${blockKey++}`}>{items}</ol>
        : <ul key={`l${blockKey++}`}>{items}</ul>,
    );
    list = null;
  };
  const flushQuote = () => {
    if (!quote.length) return;
    const body = quote.join(" ").trim();
    quote = [];
    if (!body) return;
    blocks.push(<blockquote key={`q${blockKey++}`}>{renderInline(body)}</blockquote>);
  };
  const flushAll = () => { flushParagraph(); flushList(); flushQuote(); };

  for (const line of lines) {
    if (fence) {
      if (FENCE_RE.test(line)) {
        blocks.push(<pre key={`c${blockKey++}`}><code>{fence.join("\n")}</code></pre>);
        fence = null;
      } else {
        fence.push(line);
      }
      continue;
    }

    const fenceMatch = line.match(FENCE_RE);
    if (fenceMatch) {
      flushAll();
      fence = [];
      continue;
    }

    const heading = line.match(HEADING_RE);
    if (heading) {
      flushAll();
      const level = Math.min(heading[1].length + 1, 6);
      const Tag = `h${level}`;
      blocks.push(<Tag key={`h${blockKey++}`}>{renderInline(heading[2])}</Tag>);
      continue;
    }

    const bullet = line.match(BULLET_RE);
    if (bullet) {
      flushParagraph();
      flushQuote();
      if (!list || list.ordered) flushList();
      if (!list) list = { ordered: false, items: [] };
      list.items.push(bullet[1]);
      continue;
    }

    const numbered = line.match(NUMBER_RE);
    if (numbered) {
      flushParagraph();
      flushQuote();
      if (!list || !list.ordered) flushList();
      if (!list) list = { ordered: true, items: [] };
      list.items.push(numbered[2]);
      continue;
    }

    const quoted = line.match(QUOTE_RE);
    if (quoted) {
      flushParagraph();
      flushList();
      quote.push(quoted[1]);
      continue;
    }

    if (!line.trim()) {
      flushAll();
      continue;
    }

    if (list) flushList();
    if (quote.length) flushQuote();
    paragraph.push(line.trim());
  }

  if (fence) {
    blocks.push(<pre key={`c${blockKey++}`}><code>{fence.join("\n")}</code></pre>);
  }
  flushAll();

  return <div className="markdown-body">{blocks}</div>;
}
