import { Activity, ArrowUpRight, FlaskConical, ShoppingBag } from "lucide-react";
import { useEffect, useState } from "react";

// The first thing a cold visitor sees when nothing is happening: three steps, in
// order, that make VIGIL do its job in about a minute. Hidden once dismissed; the
// help button in the top bar brings it back.

const KEY = "vigil.guide.hidden";

function readHidden(): boolean {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function showGuide() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* private mode: the event below still shows it for this visit */
  }
  window.dispatchEvent(new Event("vigil:guide"));
}

export function StartHere({ onOpenChaos }: { onOpenChaos: () => void }) {
  const [hidden, setHidden] = useState(readHidden);
  useEffect(() => {
    const show = () => setHidden(false);
    window.addEventListener("vigil:guide", show);
    return () => window.removeEventListener("vigil:guide", show);
  }, []);
  if (hidden) return null;

  const hide = () => {
    setHidden(true);
    try {
      localStorage.setItem(KEY, "1");
    } catch {
      /* fine: hidden for this visit */
    }
  };

  return (
    <section className="panel guide" aria-labelledby="guide-title">
      <div className="panel-head">
        <h2 id="guide-title">See VIGIL work in about a minute</h2>
        <button type="button" className="btn btn-ghost btn-small" onClick={hide}>Hide guide</button>
      </div>
      <p className="panel-caption">Everything is running and healthy. Break something on purpose and watch VIGIL find the real cause and fix it, on its own.</p>
      <ol className="guide-steps">
        <li className="guide-step">
          <span className="guide-num" aria-hidden="true">1</span>
          <span className="guide-icon" aria-hidden="true"><ShoppingBag size={18} /></span>
          <b>Shop like a customer</b>
          <span>Buy something in the demo store. Every purchase is a real checkout through payment and inventory.</span>
          <a className="btn btn-small" href="/store" target="_blank" rel="noreferrer">Open the store <ArrowUpRight size={15} /></a>
        </li>
        <li className="guide-step">
          <span className="guide-num" aria-hidden="true">2</span>
          <span className="guide-icon guide-icon-chaos" aria-hidden="true"><FlaskConical size={18} /></span>
          <b>Break something</b>
          <span>In Chaos Lab, hold <i>Slow down inventory</i>. Customers start failing, but every health check stays green.</span>
          <button type="button" className="btn btn-small btn-chaos" onClick={onOpenChaos}>Open Chaos Lab</button>
        </li>
        <li className="guide-step">
          <span className="guide-num" aria-hidden="true">3</span>
          <span className="guide-icon guide-icon-vigil" aria-hidden="true"><Activity size={18} /></span>
          <b>Watch it heal</b>
          <span>The map marks the real cause, the loop fills in step by step, and VIGIL restarts the right service. Then it checks customers recovered.</span>
          <span className="guide-note">No clicks needed at autonomy L2</span>
        </li>
      </ol>
    </section>
  );
}
