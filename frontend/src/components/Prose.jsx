import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

const PLUGINS = [remarkGfm];

// Every link opens away from the app, and `noreferrer` keeps the opener out of reach of
// a page the model chose. The text here is model output, which is the one input in this
// system no schema constrains.
const COMPONENTS = {
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
};

/**
 * The one renderer for agent-written prose. ADR 60.
 *
 * `remark-gfm` autolinks bare URLs, so "every URL is a link" needs no second mechanism.
 * `react-markdown` builds a React tree rather than injecting HTML, so no sanitiser sits
 * behind this.
 */
export default function Prose({ children, className = "prose" }) {
  if (!children) return null;
  return (
    <div className={className}>
      <Markdown remarkPlugins={PLUGINS} components={COMPONENTS}>
        {String(children)}
      </Markdown>
    </div>
  );
}
