import { useRef } from "react";

interface Props {
  /** "col" = vertical bar between side-by-side panels (drag left/right); "row" = horizontal bar (drag up/down). */
  axis: "col" | "row";
  value: number; // fraction of the space given to the first panel
  onChange: (v: number) => void;
  onReset: () => void;
  /** Pixel extent [start, end] of the space being split, measured at drag time. */
  bounds: () => [number, number];
  minPx: number; // smallest either side may get
  label: string;
}

/** Draggable divider. Pointer drag, double-click to reset, arrow keys (Shift = bigger steps), Home/End. */
export function Splitter({ axis, value, onChange, onReset, bounds, minPx, label }: Props) {
  const dragging = useRef(false);

  const clamp = (v: number) => {
    const [a, b] = bounds();
    const span = Math.max(1, b - a);
    const lo = Math.min(0.5, minPx / span);
    return Math.min(1 - lo, Math.max(lo, v));
  };

  function move(e: React.PointerEvent) {
    if (!dragging.current) return;
    const [a, b] = bounds();
    const pos = axis === "col" ? e.clientX : e.clientY;
    onChange(clamp((pos - a) / Math.max(1, b - a)));
  }

  function key(e: React.KeyboardEvent) {
    const step = e.shiftKey ? 0.1 : 0.02;
    const back = axis === "col" ? "ArrowLeft" : "ArrowUp";
    const fwd = axis === "col" ? "ArrowRight" : "ArrowDown";
    let v: number | null = null;
    if (e.key === back) v = value - step;
    else if (e.key === fwd) v = value + step;
    else if (e.key === "Home") v = 0;
    else if (e.key === "End") v = 1;
    else if (e.key === "Enter") {
      onReset();
      e.preventDefault();
      return;
    }
    if (v !== null) {
      onChange(clamp(v));
      e.preventDefault();
    }
  }

  return (
    <div
      className={`splitter ${axis}`}
      role="separator"
      aria-orientation={axis === "col" ? "vertical" : "horizontal"}
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value * 100)}
      tabIndex={0}
      title={`${label}: drag to resize, double-click to reset`}
      onPointerDown={(e) => {
        dragging.current = true;
        e.currentTarget.setPointerCapture(e.pointerId);
        document.body.classList.add(axis === "col" ? "resizing-col" : "resizing-row");
      }}
      onPointerMove={move}
      onPointerUp={(e) => {
        dragging.current = false;
        e.currentTarget.releasePointerCapture(e.pointerId);
        document.body.classList.remove("resizing-col", "resizing-row");
      }}
      onDoubleClick={onReset}
      onKeyDown={key}
    />
  );
}
