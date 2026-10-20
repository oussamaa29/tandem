import { useEffect, useState } from "react";
import { parseServerDate, timeAgo } from "../format";
import type { Activity } from "../types";
import { Icon } from "./ui";

export default function ActivityPanel({ activity, onClose }: { activity: Activity[]; onClose: () => void }) {
  // Rafraîchit les « il y a 2 min » sans attendre un nouvel événement
  const [now, setNow] = useState(() => Date.now());
  // Les entrées arrivées après l'ouverture du panneau sont surlignées (animation CSS jouée une fois)
  const [openedAt] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(t);
  }, []);

  return (
    <aside className="side-panel" aria-label="Activité du tableau">
      <div className="side-head">
        <span>Activité</span>
        <button className="icon-btn" onClick={onClose} aria-label="Fermer l'activité">
          <Icon name="x" />
        </button>
      </div>
      {activity.length === 0 ? (
        <p className="hint" style={{ padding: 24 }}>Rien pour l'instant.</p>
      ) : (
        <ol className="feed" aria-live="polite">
          {activity.map((a) => (
            <li key={a.id} className={parseServerDate(a.created_at).getTime() > openedAt ? "new" : ""}>
              <div>
                <strong>{a.user.name}</strong> {a.message}
                <time dateTime={a.created_at}>{timeAgo(a.created_at, now)}</time>
              </div>
            </li>
          ))}
        </ol>
      )}
    </aside>
  );
}
