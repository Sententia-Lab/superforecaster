/**
 * One labeled field on its own row.
 *
 * Multi-line by default, and the box grows to fit what is in it. Every long field here
 * holds a sentence or a paragraph — a sub-question, a population definition, an argument
 * for a weight — and a single-line input truncates all of them at the same width, which
 * makes a payload impossible to review while editing it.
 *
 * The growing is CSS (`field-sizing: content`), not a measured `scrollHeight`. Measuring
 * needs settled layout, and reading it a frame early gives a garbage height that then
 * sticks until the next keystroke.
 */
export default function EditorField({
  label,
  hint,
  value,
  onChange,
  placeholder,
  singleLine = false,
}) {
  const Box = singleLine ? "input" : "textarea";
  return (
    <label className="editor-field">
      <span className="editor-label">
        {label}
        {hint && <span className="editor-hint">{hint}</span>}
      </span>
      <Box
        className="field-input"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}
