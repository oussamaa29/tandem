import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { initials } from "../format";
import { ToastContext, type ToastTone } from "../toast";

/* ------------------------------------------------------------------ Icônes */

const PATHS = {
  plus: "M12 5v14M5 12h14",
  x: "M18 6 6 18M6 6l12 12",
  dots: "M5 12h.01M12 12h.01M19 12h.01",
  users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
  text: "M4 6h16M4 12h16M4 18h10",
  arrowLeft: "M19 12H5M12 19l-7-7 7-7",
  arrowRight: "M5 12h14M12 5l7 7-7 7",
  edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  board: "M3 3h7v18H3zM14 3h7v11h-7z",
  user: "M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}

export function Logo() {
  return (
    <span className="logo">
      tandem<span className="logo-dot" aria-hidden="true" />
    </span>
  );
}

/* ------------------------------------------------------------------ Personnes */

/** Initiales en monospace, sans couleur : identifie quelqu'un sans décor. */
export function Initials({ name, title }: { name: string; title?: string }) {
  return (
    <span className="initials" title={title ?? name} aria-label={title ?? name}>
      {initials(name)}
    </span>
  );
}

/** « 2 en ligne » + prénoms ; le point vert est le seul signal de présence. */
export function OnlineList({ people, meId }: { people: { id: number; name: string }[]; meId: number }) {
  if (people.length === 0) return null;
  const names = people.map((p) => (p.id === meId ? "toi" : p.name.split(" ")[0]));
  return (
    <span className="online" title={names.join(", ")}>
      <span className="online-dot" aria-hidden="true" />
      {people.length === 1 ? "Toi seul·e en ligne" : `${people.length} en ligne · ${names.slice(0, 3).join(", ")}${people.length > 3 ? "…" : ""}`}
    </span>
  );
}

/* ------------------------------------------------------------------ Toasts */

interface Toast {
  id: number;
  text: string;
  tone: ToastTone;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const push = useCallback((text: string, tone: Toast["tone"] = "info") => {
    const id = nextId.current++;
    setToasts((t) => [...t.slice(-2), { id, text, tone }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "error" ? 6000 : 3500);
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.tone === "error" ? "toast-error" : ""}`}>
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/* ------------------------------------------------------------------ Menu */

export interface MenuItem {
  label: string;
  icon?: IconName;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  separatorBefore?: boolean;
}

export function Menu({ label, items }: { label: string; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    wrap.current?.querySelector<HTMLButtonElement>(".menu button:not(:disabled)")?.focus();
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="menu-wrap" ref={wrap}>
      <button className="icon-btn" aria-label={label} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon name="dots" />
      </button>
      {open && (
        <div className="menu" role="menu">
          {items.map((item) => (
            <div key={item.label}>
              {item.separatorBefore && <hr />}
              <button
                role="menuitem"
                className={item.danger ? "danger" : ""}
                disabled={item.disabled}
                onClick={() => {
                  setOpen(false);
                  item.onSelect();
                }}
              >
                {item.icon && <Icon name={item.icon} size={15} />}
                {item.label}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ Dialogue natif */

/** <dialog> natif : piège à focus, Échap et arrière-plan inerte fournis par le navigateur. */
export function Modal({ onClose, label, children }: { onClose: () => void; label: string; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  // Le clic doit avoir commencé ET fini sur l'arrière-plan : sélectionner du texte dans un champ
  // en relâchant la souris hors de la boîte ne doit pas fermer la modale
  const downOnBackdrop = useRef(false);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className="modal"
      aria-label={label}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onPointerDown={(e) => {
        downOnBackdrop.current = e.target === ref.current;
      }}
      onClick={(e) => {
        if (downOnBackdrop.current && e.target === ref.current) onClose(); // clic sur l'arrière-plan
        downOnBackdrop.current = false;
      }}
    >
      {children}
    </dialog>
  );
}
