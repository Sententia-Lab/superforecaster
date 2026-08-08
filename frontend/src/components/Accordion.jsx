/**
 * A collapsible block. Native `<details>` so open state, keyboard, and find-in-page all
 * work without a line of JavaScript.
 *
 * `summary` is the always-visible row. Put controls in it freely — a button inside a
 * `<summary>` must call `e.stopPropagation()` or clicking it also toggles the block.
 */
export default function Accordion({
  summary,
  children,
  defaultOpen = false,
  className = "",
}) {
  return (
    <details className={`acc ${className}`.trim()} open={defaultOpen}>
      <summary>{summary}</summary>
      <div className="acc-body">{children}</div>
    </details>
  );
}
