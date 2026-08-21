import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

const PLUGINS = [remarkGfm];

// What the research store wraps a search hit in — SQLite's `highlight()` put them there.
// STX and ETX, so neither JSON nor markdown can mistake them for syntax.
export const MARK_START = "\u0002";
export const MARK_END = "\u0003";

/** One string with markers in it, as `mark` elements and plain text. */
function splitMarks(value) {
  if (!value.includes(MARK_START)) return null;
  const out = [];
  let rest = value;
  for (;;) {
    const start = rest.indexOf(MARK_START);
    if (start === -1) break;
    const end = rest.indexOf(MARK_END, start + 1);
    if (end === -1) break;
    if (start > 0) out.push({ type: "text", value: rest.slice(0, start) });
    out.push({
      type: "element",
      tagName: "mark",
      properties: {},
      children: [{ type: "text", value: rest.slice(start + 1, end) }],
    });
    rest = rest.slice(end + 1);
  }
  // Any unpaired marker is stripped rather than shown: a stray control character on
  // screen would be a worse bug than a missing highlight.
  if (rest) out.push({ type: "text", value: rest.split(MARK_START).join("") });
  return out;
}

/**
 * Turn the store's markers into `<mark>`, after markdown has been parsed.
 *
 * A rehype pass rather than a string replace, because the text it walks has already been
 * through the markdown parser — so a marker can never be read as syntax, and a hit
 * spanning a bold run or a list item still marks the words rather than breaking the
 * markup around them.
 */
function rehypeMarks() {
  return (tree) => {
    const walk = (node) => {
      if (!node.children) return;
      const next = [];
      for (const child of node.children) {
        if (child.type === "text") {
          const parts = splitMarks(child.value);
          if (parts) {
            next.push(...parts);
            continue;
          }
        }
        walk(child);
        next.push(child);
      }
      node.children = next;
    };
    walk(tree);
  };
}

const MARK_PLUGINS = [rehypeMarks];

// Every link opens away from the app, and `noreferrer` keeps the opener out of reach of
// a page the model chose. The text here is model output, which is the one input in this
// system no schema constrains.
const COMPONENTS = {
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
};

// Headings and paragraphs collapse to plain inline text. A stored page title is one
// line in a list, and a scraped one often arrives with a stray `#` or `**` on it —
// rendering that as an H1 breaks the row rather than reading it.
const INLINE = {
  ...COMPONENTS,
  p: ({ node, ...props }) => <span {...props} />,
  h1: ({ node, ...props }) => <span {...props} />,
  h2: ({ node, ...props }) => <span {...props} />,
  h3: ({ node, ...props }) => <span {...props} />,
  h4: ({ node, ...props }) => <span {...props} />,
  h5: ({ node, ...props }) => <span {...props} />,
  h6: ({ node, ...props }) => <span {...props} />,
};

/**
 * The one renderer for agent-written prose. ADR 60.
 *
 * `remark-gfm` autolinks bare URLs, so "every URL is a link" needs no second mechanism.
 * `react-markdown` builds a React tree rather than injecting HTML, so no sanitiser sits
 * behind this.
 *
 * `inline` renders into a span with block elements flattened, for a title or a label
 * that has to stay one line. `marks` turns the research store's search markers into
 * `<mark>` (§4b).
 */
export default function Prose({
  children,
  className = "prose",
  inline = false,
  marks = false,
}) {
  if (!children) return null;
  const Tag = inline ? "span" : "div";
  return (
    <Tag className={className}>
      <Markdown
        remarkPlugins={PLUGINS}
        rehypePlugins={marks ? MARK_PLUGINS : undefined}
        components={inline ? INLINE : COMPONENTS}
      >
        {String(children)}
      </Markdown>
    </Tag>
  );
}

/** The same markers in a plain string — a URL, which is not markdown. */
export function Marked({ children }) {
  const value = String(children ?? "");
  if (!value.includes(MARK_START)) return value;
  return value.split(MARK_START).map((chunk, i) => {
    if (i === 0) return chunk;
    const [hit, ...rest] = chunk.split(MARK_END);
    return (
      <span key={i}>
        <mark>{hit}</mark>
        {rest.join(MARK_END)}
      </span>
    );
  });
}
