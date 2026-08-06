import { useEffect, useRef } from "react";

/** The raw streaming tail — used ONLY inside the decompose section. */
export default function LiveTail({ text }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text]);
  return (
    <div className="tail" ref={ref}>
      {text || "…"}
    </div>
  );
}
