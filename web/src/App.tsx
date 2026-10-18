import { BottomTabs } from "./components/BottomTabs";
import { Header } from "./components/Header";
import { MarketPanel } from "./components/MarketPanel";
import { ScannerTable } from "./components/ScannerTable";
import { useStore } from "./store";

export function App() {
  const conn = useStore((s) => s.conn);
  const killed = useStore((s) => s.status?.state === "killed");
  return (
    <div className={`app ${killed ? "is-killed" : ""}`}>
      <Header />
      {conn !== "open" && <div className="banner">Reconnecting to the engine… (data on screen may be stale)</div>}
      <main className="main">
        <ScannerTable />
        <MarketPanel />
      </main>
      <BottomTabs />
    </div>
  );
}
