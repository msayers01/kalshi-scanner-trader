import { useEffect, useRef, useState } from "react";
import { BottomTabs } from "./components/BottomTabs";
import { Header } from "./components/Header";
import { Help } from "./components/Help";
import { MarketPanel } from "./components/MarketPanel";
import { ScannerTable } from "./components/ScannerTable";
import { Splitter } from "./components/Splitter";
import { useStore } from "./store";

const LAYOUT_KEY = "kst.layout";
const DEFAULT_LAYOUT = { main: 0.64, left: 0.62 }; // main = top area vs bottom tabs; left = scanner vs market

function loadLayout(): typeof DEFAULT_LAYOUT {
  try {
    const l = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "{}") as Partial<typeof DEFAULT_LAYOUT>;
    const ok = (v: unknown) => typeof v === "number" && v > 0.05 && v < 0.95;
    return { main: ok(l.main) ? (l.main as number) : DEFAULT_LAYOUT.main, left: ok(l.left) ? (l.left as number) : DEFAULT_LAYOUT.left };
  } catch {
    return DEFAULT_LAYOUT;
  }
}

export function App() {
  const conn = useStore((s) => s.conn);
  const killed = useStore((s) => s.status?.state === "killed");
  const help = useStore((s) => s.help);
  const openHelp = useStore((s) => s.openHelp);
  const [layout, setLayout] = useState(loadLayout);
  const mainRef = useRef<HTMLElement>(null);
  const appRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch {
      /* storage blocked: layout lasts for this page only */
    }
  }, [layout]);

  // "?" opens the guide (unless typing in a field).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key === "?" && !(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable))) openHelp();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [openHelp]);

  const style = {
    "--main-fr": `${layout.main}fr`,
    "--bottom-fr": `${1 - layout.main}fr`,
    "--left-fr": `${layout.left}fr`,
    "--right-fr": `${1 - layout.left}fr`,
  } as React.CSSProperties;

  return (
    <div ref={appRef} className={`app ${killed ? "is-killed" : ""}`} style={style}>
      <Header />
      {conn !== "open" && <div className="banner">Reconnecting to the engine… (data on screen may be stale)</div>}
      <main ref={mainRef} className="main">
        <ScannerTable />
        <Splitter
          axis="col"
          label="Scanner / market panel divider"
          value={layout.left}
          minPx={300}
          bounds={() => {
            const r = mainRef.current?.getBoundingClientRect();
            return r ? [r.left, r.right] : [0, 1];
          }}
          onChange={(left) => setLayout((l) => ({ ...l, left }))}
          onReset={() => setLayout((l) => ({ ...l, left: DEFAULT_LAYOUT.left }))}
        />
        <MarketPanel />
      </main>
      <Splitter
        axis="row"
        label="Top panels / bottom tabs divider"
        value={layout.main}
        minPx={140}
        bounds={() => {
          const m = mainRef.current?.getBoundingClientRect();
          const a = appRef.current?.getBoundingClientRect();
          return m && a ? [m.top, a.bottom - 6] : [0, 1];
        }}
        onChange={(main) => setLayout((l) => ({ ...l, main }))}
        onReset={() => setLayout((l) => ({ ...l, main: DEFAULT_LAYOUT.main }))}
      />
      <BottomTabs />
      {help !== null && <Help onResetLayout={() => setLayout(DEFAULT_LAYOUT)} />}
    </div>
  );
}
