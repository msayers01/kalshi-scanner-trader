import { cents, qty } from "../format";
import type { Book } from "../types";

/** YES-price ladder: asks (sell YES / buy NO) on top, bids below. Click a level to prefill the ticket. */
export function OrderBook({ book, onPick }: { book: Book | null; onPick?: (yesPrice: number) => void }) {
  if (!book) return <div className="empty">Loading order book…</div>;
  const maxQ = Math.max(1, ...book.bids.map((b) => b[1]), ...book.asks.map((a) => a[1]));
  const asks = [...book.asks].slice(0, 8).reverse();
  const bids = book.bids.slice(0, 8);
  const spread = book.asks[0] && book.bids[0] ? book.asks[0][0] - book.bids[0][0] : null;
  return (
    <div className={`book ${book.fresh ? "" : "stale"}`}>
      <div className="book-row head">
        <span>YES price</span>
        <span>Size</span>
      </div>
      {asks.map(([p, q]) => (
        <div key={`a${p}`} className="book-row ask" onClick={() => onPick?.(p)}>
          <span>{cents(p)}¢</span>
          <span>{qty(q)}</span>
          <i style={{ width: `${(q / maxQ) * 100}%` }} />
        </div>
      ))}
      <div className="book-mid">
        mid {book.mid !== null ? `${cents(book.mid)}¢` : "—"} · spread {spread !== null ? `${cents(spread)}¢` : "—"}
        {!book.fresh && <span className="neg"> · stale</span>}
      </div>
      {bids.map(([p, q]) => (
        <div key={`b${p}`} className="book-row bid" onClick={() => onPick?.(p)}>
          <span>{cents(p)}¢</span>
          <span>{qty(q)}</span>
          <i style={{ width: `${(q / maxQ) * 100}%` }} />
        </div>
      ))}
    </div>
  );
}
