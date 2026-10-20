import { useEffect, useRef, useState, type Dispatch } from "react";
import { api, ApiError } from "../api";
import type { BoardAction } from "../boardState";
import { timeAgo } from "../format";
import type { Card, Member, PresenceUser } from "../types";
import { Icon, Modal } from "./ui";
import { useToast } from "../toast";

interface Props {
  card: Card;
  columnName: string;
  members: Member[];
  otherEditors: PresenceUser[];
  dispatch: Dispatch<BoardAction>;
  onClose: () => void;
}

type Field = "title" | "description";
type SaveState = "idle" | "saving" | "saved";

/**
 * Édition d'une carte.
 *
 * Verrou optimiste : on retient la version de la carte au moment où l'on commence à modifier un
 * champ (`baseVersion`). Si quelqu'un d'autre enregistre entre-temps, le serveur répond 409 et on
 * propose explicitement de garder sa version ou la nôtre — jamais d'écrasement silencieux.
 * Tant qu'un champ n'est pas en cours d'édition, il suit en direct les modifications des autres.
 */
export default function CardModal({ card, columnName, members, otherEditors, dispatch, onClose }: Props) {
  const toast = useToast();
  const [title, setTitle] = useState(card.title);
  const [description, setDescription] = useState(card.description);
  const [dirty, setDirty] = useState<Record<Field, boolean>>({ title: false, description: false });
  const [conflict, setConflict] = useState<Card | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const baseVersion = useRef<number | null>(null);
  const titleRef = useRef<HTMLTextAreaElement>(null);
  // Copies synchrones des brouillons et des drapeaux « modifié » : un enregistrement qui se termine
  // les lit tels qu'ils sont à cet instant, pas tels qu'ils étaient quand il est parti
  const drafts = useRef({ title: card.title, description: card.description });
  const dirtyRef = useRef<Record<Field, boolean>>({ title: false, description: false });
  // Enregistrement en cours : les suivants l'attendent au lieu de partir avec la même version de
  // base (ils seraient refusés en 409 par notre propre modification)
  const inFlight = useRef<Promise<boolean> | null>(null);
  const lastOwnSave = useRef<{ from: number; to: number } | null>(null);

  const updateDirty = (next: Record<Field, boolean>) => {
    dirtyRef.current = next;
    setDirty(next);
  };

  // Les champs non modifiés localement suivent les mises à jour distantes
  // (ajustement pendant le rendu, le motif recommandé par React plutôt qu'un effet)
  const [synced, setSynced] = useState({ title: card.title, description: card.description });
  if (synced.title !== card.title || synced.description !== card.description) {
    setSynced({ title: card.title, description: card.description });
    if (!dirty.title) setTitle(card.title);
    if (!dirty.description) setDescription(card.description);
  }

  useEffect(() => {
    const el = titleRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight}px`;
    }
  }, [title]);

  const edit = (field: Field, value: string) => {
    if (!dirtyRef.current.title && !dirtyRef.current.description) baseVersion.current = card.version;
    drafts.current = { ...drafts.current, [field]: value };
    updateDirty({ ...dirtyRef.current, [field]: true });
    if (field === "title") setTitle(value);
    else setDescription(value);
  };

  /** Résout à true si l'enregistrement a réussi. Les enregistrements sont faits l'un après l'autre. */
  const save = (fields: Partial<Pick<Card, "title" | "description" | "assignee_id">>, version: number): Promise<boolean> => {
    const previous = inFlight.current;
    const run = (async () => {
      if (previous) await previous;
      // Préparé pendant que notre enregistrement précédent était en cours, sur la même version :
      // on repart de celle qu'il a produite (c'était notre propre modification, pas un conflit)
      const own = lastOwnSave.current;
      if (own && own.from === version) version = own.to;
      setSaveState("saving");
      try {
        const updated = await api.updateCard(card.id, { version, ...fields });
        lastOwnSave.current = { from: version, to: updated.version };
        dispatch({ type: "card/upsert", card: updated });
        // Un champ n'est « enregistré » que si son brouillon n'a pas bougé depuis l'envoi : ce qui a
        // été tapé pendant la requête reste un brouillon (et n'est pas écrasé par la valeur serveur)
        const d = dirtyRef.current;
        const next = {
          title: d.title && !("title" in fields && drafts.current.title.trim() === fields.title),
          description: d.description && !("description" in fields && drafts.current.description === fields.description),
        };
        updateDirty(next);
        // Un brouillon encore ouvert (ex. on a changé l'assignation en pleine rédaction) repart de
        // notre propre nouvelle version, sinon notre propre modification déclencherait un conflit.
        // (Seulement si personne d'autre n'a modifié la carte depuis le début du brouillon.)
        if (!next.title && !next.description) baseVersion.current = null;
        else if (baseVersion.current === version) baseVersion.current = updated.version;
        setConflict(null);
        setSaveState("saved");
        return true;
      } catch (e) {
        setSaveState("idle");
        if (e instanceof ApiError && e.status === 409) {
          setConflict((e.data as { card: Card }).card);
        } else {
          toast((e as Error).message, "error");
        }
        return false;
      }
    })();
    inFlight.current = run;
    run.then(() => {
      if (inFlight.current === run) inFlight.current = null;
    });
    return run;
  };

  const commit = (field: Field) => {
    if (!dirtyRef.current[field]) return;
    if (field === "title") {
      const t = title.trim();
      if (!t) {
        setTitle(card.title);
        drafts.current = { ...drafts.current, title: card.title };
        updateDirty({ ...dirtyRef.current, title: false });
        return;
      }
      if (t === card.title) return updateDirty({ ...dirtyRef.current, title: false });
      save({ title: t, ...(dirtyRef.current.description ? { description } : {}) }, baseVersion.current ?? card.version);
    } else {
      if (description === card.description) return updateDirty({ ...dirtyRef.current, description: false });
      save({ description, ...(dirtyRef.current.title && title.trim() ? { title: title.trim() } : {}) }, baseVersion.current ?? card.version);
    }
  };

  const takeTheirs = () => {
    if (!conflict) return;
    dispatch({ type: "card/upsert", card: conflict });
    setTitle(conflict.title);
    setDescription(conflict.description);
    drafts.current = { title: conflict.title, description: conflict.description };
    updateDirty({ title: false, description: false });
    baseVersion.current = null;
    setConflict(null);
  };

  const keepMine = () => {
    if (!conflict) return;
    baseVersion.current = conflict.version; // on repart de sa version, en connaissance de cause
    save({ title: title.trim() || conflict.title, description }, conflict.version);
  };

  const remove = async () => {
    try {
      await api.deleteCard(card.id);
      dispatch({ type: "card/remove", id: card.id });
      onClose();
      toast("Carte supprimée");
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const requestClose = async () => {
    // Enregistre ce qui est en cours avant de fermer, et ne ferme qu'une fois l'enregistrement
    // réussi : sur un conflit ou une erreur, la modale reste ouverte pour ne pas perdre le brouillon
    if (conflict) return toast("Choisis d'abord la version à garder.", "error");
    // Ex. « Fermer » cliqué depuis un champ : le blur a déjà lancé l'enregistrement, on l'attend
    if (inFlight.current && !(await inFlight.current)) return;
    const d = dirtyRef.current;
    const t = drafts.current.title.trim();
    const fields = {
      ...(d.title && t ? { title: t } : {}),
      ...(d.description ? { description: drafts.current.description } : {}),
    };
    if (Object.keys(fields).length && !(await save(fields, baseVersion.current ?? card.version))) return;
    onClose();
  };

  const creator = members.find((m) => m.id === card.created_by_id);
  const sameCardEditors = otherEditors.map((e) => e.name.split(" ")[0]).join(", ");

  return (
    <Modal onClose={requestClose} label={`Carte ${card.title}`}>
      <div className="modal-head">
        <textarea
          ref={titleRef}
          className="title-input"
          rows={1}
          maxLength={200}
          value={title}
          aria-label="Titre"
          onChange={(e) => edit("title", e.target.value.replace(/\n/g, ""))}
          onBlur={() => commit("title")}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              e.currentTarget.blur();
            }
          }}
        />
        <button className="icon-btn" onClick={requestClose} aria-label="Fermer">
          <Icon name="x" />
        </button>
      </div>

      <div className="modal-body">
        <p style={{ color: "var(--text-3)", fontSize: 12.5, marginTop: -10 }}>
          Dans <strong style={{ color: "var(--text-2)" }}>{columnName}</strong>
          {creator && <> · créée par {creator.name}</>}
          {card.updated_at && <> · modifiée {timeAgo(card.updated_at)}</>}
        </p>

        {conflict && (
          <div className="banner banner-warn" role="alert">
            <span className="grow">Quelqu'un a modifié cette carte pendant que tu écrivais.</span>
            <button className="btn" onClick={takeTheirs}>Voir sa version</button>
            <button className="btn btn-primary" onClick={keepMine}>Garder la mienne</button>
          </div>
        )}
        {!conflict && otherEditors.length > 0 && (
          <div className="banner banner-live" role="status">
            <span className="online-dot" aria-hidden="true" />
            <span>{sameCardEditors} {otherEditors.length > 1 ? "modifient" : "modifie"} aussi cette carte.</span>
          </div>
        )}

        <dl className="meta-grid">
          <dt>Assignée à</dt>
          <dd>
            <select
              className="select"
              aria-label="Assignée à"
              value={card.assignee_id ?? ""}
              onChange={(e) => save({ assignee_id: e.target.value ? Number(e.target.value) : null }, card.version)}
            >
              <option value="">Personne</option>
              {members.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          </dd>
        </dl>

        <div>
          <div className="section-label" style={{ display: "flex", justifyContent: "space-between" }}>
            <span>Description</span>
            <span className="save-state" aria-live="polite">
              {saveState === "saving" ? "Enregistrement…" : dirty.description ? "Modifications non enregistrées" : saveState === "saved" ? "Enregistré" : ""}
            </span>
          </div>
          <textarea
            className="input"
            rows={6}
            maxLength={10000}
            placeholder="Ajoute du contexte, des liens, une checklist…"
            value={description}
            aria-label="Description"
            onChange={(e) => edit("description", e.target.value)}
            onBlur={() => commit("description")}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) e.currentTarget.blur();
            }}
          />
          <p className="hint" style={{ textAlign: "left", marginTop: 6 }}>
            Enregistrement automatique · <kbd className="kbd">Ctrl</kbd> + <kbd className="kbd">Entrée</kbd>
          </p>
        </div>
      </div>

      <div className="modal-foot">
        {confirmDelete ? (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ fontSize: 13 }}>Supprimer définitivement ?</span>
            <button className="btn btn-danger" onClick={remove} autoFocus>Supprimer</button>
            <button className="btn btn-ghost" onClick={() => setConfirmDelete(false)}>Annuler</button>
          </div>
        ) : (
          <button className="btn btn-ghost btn-danger" onClick={() => setConfirmDelete(true)}>
            <Icon name="trash" size={15} /> Supprimer
          </button>
        )}
        <button className="btn" onClick={requestClose}>Fermer</button>
      </div>
    </Modal>
  );
}
