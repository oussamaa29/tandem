import {
  closestCorners, DndContext, DragOverlay, KeyboardSensor, PointerSensor, useSensor, useSensors,
  type Announcements, type DragEndEvent, type DragOverEvent, type DragStartEvent,
} from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api } from "../api";
import { findCard } from "../boardState";
import { parseCardId, parseColumnId } from "../dnd";
import { navigate } from "../router";
import type { Board, Card, Column, ServerEvent, User } from "../types";
import { useBoard, type BoardEnd } from "../useBoard";
import ActivityPanel from "./ActivityPanel";
import { CardBody } from "./CardItem";
import CardModal from "./CardModal";
import ColumnView from "./ColumnView";
import MembersDialog from "./MembersDialog";
import { Icon, Logo, Menu, Modal, OnlineList } from "./ui";
import { useToast } from "../toast";

const CONNECTION_LABELS = {
  connecting: "Connexion…",
  live: "En direct",
  reconnecting: "Reconnexion…",
  offline: "Hors ligne",
} as const;


function locate(board: Board, cardId: number): { columnId: number; index: number } | null {
  for (const col of board.columns) {
    const index = col.cards.findIndex((c) => c.id === cardId);
    if (index !== -1) return { columnId: col.id, index };
  }
  return null;
}

interface Props {
  boardId: number;
  me: User;
  onLogout: () => void;
}

export default function BoardView({ boardId, me, onLogout }: Props) {
  const toast = useToast();
  const [openCardId, setOpenCardId] = useState<number | null>(null);
  const [showActivity, setShowActivity] = useState(() => window.innerWidth >= 1280);
  const [showMembers, setShowMembers] = useState(false);
  const [flashIds, setFlashIds] = useState<Set<number>>(new Set());
  const [pendingDelete, setPendingDelete] = useState<Column | null>(null);
  const membersRef = useRef<Map<number, string>>(new Map());
  const openCardRef = useRef<number | null>(null);
  useLayoutEffect(() => {
    openCardRef.current = openCardId;
  }, [openCardId]);

  const onEnd = useCallback(
    (reason: BoardEnd) => {
      const messages: Record<BoardEnd, string> = {
        deleted: "Ce tableau a été supprimé par son propriétaire.",
        removed: "Tu ne fais plus partie de ce tableau.",
        "not-found": "Ce tableau n'existe pas ou tu n'y as pas accès.",
        unauthorized: "Ta session a expiré, reconnecte-toi.",
      };
      toast(messages[reason], "error");
      if (reason === "unauthorized") onLogout();
      else navigate("/");
    },
    [toast, onLogout],
  );

  const flash = useCallback((ids: number[]) => {
    setFlashIds((s) => new Set([...s, ...ids]));
    window.setTimeout(() => {
      setFlashIds((s) => {
        const next = new Set(s);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    }, 1600);
  }, []);

  const onRemoteEvent = useCallback(
    (event: ServerEvent) => {
      const who = membersRef.current.get(event.actor_id) ?? "Quelqu'un";
      switch (event.type) {
        case "card.created":
        case "card.updated":
          flash([event.payload.id]);
          break;
        case "card.moved":
          flash([event.payload.card.id]);
          break;
        case "card.deleted":
          if (openCardRef.current === event.payload.id) {
            setOpenCardId(null);
            toast(`${who} a supprimé la carte que tu consultais.`);
          }
          break;
        case "column.deleted":
          toast(`${who} a supprimé une colonne.`);
          break;
      }
    },
    [flash, toast],
  );

  const { board, dispatch, status, presence, activity, loadError, resync, sendEditing } = useBoard({
    boardId,
    meId: me.id,
    onRemoteEvent,
    onEnd,
  });

  useEffect(() => {
    if (board) membersRef.current = new Map(board.members.map((m) => [m.id, m.name.split(" ")[0]]));
  }, [board]);

  const boardName = board?.name;
  useEffect(() => {
    document.title = boardName ? `${boardName} · Tandem` : "Tandem";
    return () => {
      document.title = "Tandem";
    };
  }, [boardName]);

  // Si la carte ouverte disparaît (colonne supprimée, resynchronisation…), la modale se ferme d'elle-même
  const openCard = findCard(board, openCardId);
  const editingId = openCard ? openCard.id : null;

  // Signale aux autres quelle carte on édite ; renvoyé après chaque reconnexion
  useEffect(() => {
    if (status === "live") sendEditing(editingId);
  }, [editingId, status, sendEditing]);

  const members = useMemo(() => new Map((board?.members ?? []).map((m) => [m.id, m])), [board?.members]);
  const editors = useMemo(
    () => new Map(presence.filter((p) => p.id !== me.id && p.editing_card_id !== null).map((p) => [p.editing_card_id!, p])),
    [presence, me.id],
  );

  /* ------------------------------------------------------------ Glisser-déposer */

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space", "Enter"] },
    }),
  );
  // Les handlers dnd-kit lisent l'état le plus récent, y compris entre deux rendus
  const boardRef = useRef(board);
  useLayoutEffect(() => {
    boardRef.current = board;
  }, [board]);
  const dragOrigin = useRef<{ cardId: number; columnId: number; index: number } | null>(null);
  const lastDragEnd = useRef(0);
  const [activeCardId, setActiveCardId] = useState<number | null>(null);
  const [overColumnId, setOverColumnId] = useState<number | null>(null);

  const columnOf = (dndId: string | number): number | null => {
    const b = boardRef.current;
    if (!b) return null;
    const colId = parseColumnId(dndId);
    if (colId !== null) return colId;
    const cardId = parseCardId(dndId);
    return cardId !== null ? locate(b, cardId)?.columnId ?? null : null;
  };

  const onDragStart = ({ active }: DragStartEvent) => {
    const cardId = parseCardId(active.id);
    const b = boardRef.current;
    if (cardId === null || !b) return;
    const loc = locate(b, cardId);
    if (!loc) return;
    dragOrigin.current = { cardId, ...loc };
    setActiveCardId(cardId);
  };

  // Changement de colonne pendant le glisser : on déplace la carte tout de suite pour l'aperçu
  const onDragOver = ({ active, over }: DragOverEvent) => {
    const b = boardRef.current;
    const cardId = parseCardId(active.id);
    if (!b || cardId === null || !over) return;
    const from = locate(b, cardId);
    const toColumnId = columnOf(over.id);
    setOverColumnId(toColumnId);
    if (!from || toColumnId === null || from.columnId === toColumnId) return;

    const target = b.columns.find((c) => c.id === toColumnId)!;
    let index = target.cards.length;
    const overCardId = parseCardId(over.id);
    if (overCardId !== null) {
      const overIndex = target.cards.findIndex((c) => c.id === overCardId);
      const translated = active.rect.current.translated;
      const below = translated ? translated.top > over.rect.top + over.rect.height / 2 : false;
      index = overIndex + (below ? 1 : 0);
    }
    dispatch({ type: "card/move", id: cardId, columnId: toColumnId, index });
  };

  const onDragEnd = ({ over }: DragEndEvent) => {
    const origin = dragOrigin.current;
    dragOrigin.current = null;
    lastDragEnd.current = Date.now();
    setActiveCardId(null);
    setOverColumnId(null);
    const b = boardRef.current;
    if (!origin || !b) return;
    const current = locate(b, origin.cardId);
    if (!current) return;

    let finalIndex = current.index;
    const overCardId = over ? parseCardId(over.id) : null;
    if (overCardId !== null && overCardId !== origin.cardId) {
      const overLoc = locate(b, overCardId);
      if (overLoc && overLoc.columnId === current.columnId) finalIndex = overLoc.index;
    }
    if (finalIndex !== current.index) {
      dispatch({ type: "card/move", id: origin.cardId, columnId: current.columnId, index: finalIndex });
    }
    if (current.columnId === origin.columnId && finalIndex === origin.index) return;

    api.moveCard(origin.cardId, current.columnId, finalIndex).then(
      (card) => dispatch({ type: "card/upsert", card }),
      (e) => {
        toast(`Déplacement annulé : ${(e as Error).message}`, "error");
        resync();
      },
    );
  };

  const onDragCancel = () => {
    const origin = dragOrigin.current;
    dragOrigin.current = null;
    setActiveCardId(null);
    setOverColumnId(null);
    if (origin) dispatch({ type: "card/move", id: origin.cardId, columnId: origin.columnId, index: origin.index });
  };

  const announcements: Announcements = {
    onDragStart: ({ active }) => `Carte « ${titleOf(active.id)} » saisie.`,
    onDragOver: ({ over }) => (over ? `Au-dessus de ${describe(over.id)}.` : "Hors d'une colonne."),
    onDragEnd: ({ over }) => (over ? `Carte déposée dans ${describe(over.id)}.` : "Déplacement annulé."),
    onDragCancel: () => "Déplacement annulé.",
  };
  const titleOf = (id: string | number) => {
    const cid = parseCardId(id);
    return findCard(boardRef.current, cid)?.title ?? "";
  };
  const describe = (id: string | number) => {
    const colId = columnOf(id);
    const col = boardRef.current?.columns.find((c) => c.id === colId);
    return col ? `la colonne ${col.name}` : "une colonne";
  };

  /* ------------------------------------------------------------ Actions */

  const openCardSafely = (id: number) => {
    if (Date.now() - lastDragEnd.current < 200) return; // évite d'ouvrir la carte en lâchant un glisser
    setOpenCardId(id);
  };

  const addCard = async (columnId: number, title: string) => {
    const col = boardRef.current?.columns.find((c) => c.id === columnId);
    if (!col) return;
    const tempId = -Date.now() - Math.floor(Math.random() * 1000);
    const optimistic: Card = {
      id: tempId, column_id: columnId, title, description: "", assignee_id: null, version: 0,
      position: (col.cards.at(-1)?.position ?? 0) + 1024, created_by_id: me.id, updated_at: null, pending: true,
    };
    dispatch({ type: "card/upsert", card: optimistic });
    try {
      const card = await api.createCard(columnId, title);
      dispatch({ type: "card/upsert", card, replaceId: tempId });
    } catch (e) {
      dispatch({ type: "card/remove", id: tempId });
      toast((e as Error).message, "error");
    }
  };

  const renameColumn = async (columnId: number, name: string) => {
    const col = boardRef.current?.columns.find((c) => c.id === columnId);
    if (!col) return;
    dispatch({ type: "column/upsert", column: { ...col, name } });
    try {
      await api.renameColumn(columnId, name);
    } catch (e) {
      toast((e as Error).message, "error");
      resync();
    }
  };

  const moveColumn = async (columnId: number, index: number) => {
    try {
      const col = await api.moveColumn(columnId, index);
      dispatch({ type: "column/upsert", column: col });
    } catch (e) {
      toast((e as Error).message, "error");
      resync();
    }
  };

  const deleteColumn = async (column: Column) => {
    setPendingDelete(null);
    dispatch({ type: "column/remove", id: column.id });
    try {
      await api.deleteColumn(column.id);
      toast(`Colonne « ${column.name} » supprimée`);
    } catch (e) {
      toast((e as Error).message, "error");
      resync();
    }
  };

  const requestDeleteColumn = (column: Column) => {
    if (column.cards.length === 0) deleteColumn(column);
    else setPendingDelete(column);
  };

  /* ------------------------------------------------------------ Rendu */

  const activeCard = findCard(board, activeCardId);

  return (
    <>
      <header className="topbar">
        <a href="#/" aria-label="Retour aux tableaux">
          <Logo />
        </a>
        <span className="crumb-sep hide-mobile">/</span>
        {board ? (
          <BoardTitle name={board.name} onRename={async (name) => {
            const previous = board.name;
            dispatch({ type: "board/rename", name });
            try {
              await api.renameBoard(board.id, name);
            } catch (e) {
              dispatch({ type: "board/rename", name: previous });
              toast((e as Error).message, "error");
            }
          }} />
        ) : (
          <span className="skeleton" style={{ width: 140, height: 20 }} />
        )}
        <div className="spacer" />
        {/* En direct : on affiche qui est là ; sinon l'état de la connexion */}
        <span role="status" aria-live="polite">
          {status === "live" && presence.length > 0 ? (
            <OnlineList people={presence} meId={me.id} />
          ) : (
            <span className={`conn conn-${status}`}>{CONNECTION_LABELS[status]}</span>
          )}
        </span>
        {board && (
          <button className="btn" onClick={() => setShowMembers(true)}>
            <Icon name="users" size={15} />
            <span className="hide-mobile">Partager</span>
            <span className="badge">{board.members.length}</span>
          </button>
        )}
        <button
          className={`btn ${showActivity ? "btn-primary" : ""}`}
          onClick={() => setShowActivity((s) => !s)}
          aria-pressed={showActivity}
          aria-label="Activité"
        >
          <Icon name="activity" size={15} />
        </button>
        <Menu
          label="Compte"
          items={[
            { label: "Mes tableaux", icon: "board", onSelect: () => navigate("/") },
            { label: "Se déconnecter", icon: "logout", onSelect: onLogout, separatorBefore: true },
          ]}
        />
      </header>

      <div className="board-shell">
        {loadError && !board ? (
          <div className="empty-state" style={{ flex: 1 }}>
            <h2>Impossible de charger le tableau</h2>
            <p>{loadError}</p>
            <button className="btn btn-primary" onClick={resync}>Réessayer</button>
          </div>
        ) : !board ? (
          <div className="board-scroll" aria-busy="true">
            {[180, 120, 220].map((h, i) => (
              <div key={i} className="column" style={{ padding: 12, gap: 8 }}>
                <div className="skeleton" style={{ height: 16, width: "50%" }} />
                <div className="skeleton" style={{ height: h }} />
              </div>
            ))}
          </div>
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCorners}
            onDragStart={onDragStart}
            onDragOver={onDragOver}
            onDragEnd={onDragEnd}
            onDragCancel={onDragCancel}
            accessibility={{
              announcements,
              screenReaderInstructions: {
                draggable: "Espace pour saisir la carte, flèches pour la déplacer, Espace pour la déposer, Échap pour annuler.",
              },
            }}
          >
            <div className="board-scroll">
              {board.columns.map((col, i) => (
                <ColumnView
                  key={col.id}
                  column={col}
                  index={i}
                  columnCount={board.columns.length}
                  members={members}
                  editors={editors}
                  flashIds={flashIds}
                  isDropTarget={activeCardId !== null && overColumnId === col.id}
                  onOpenCard={openCardSafely}
                  onAddCard={addCard}
                  onRename={renameColumn}
                  onMove={moveColumn}
                  onDelete={requestDeleteColumn}
                />
              ))}
              <AddColumn boardId={board.id} onCreated={(col) => dispatch({ type: "column/upsert", column: col })} />
            </div>
            <DragOverlay dropAnimation={{ duration: 180, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" }}>
              {activeCard && (
                <div className="card is-overlay">
                  <CardBody card={activeCard} assignee={activeCard.assignee_id ? members.get(activeCard.assignee_id) : undefined} />
                </div>
              )}
            </DragOverlay>
          </DndContext>
        )}

        {showActivity && <ActivityPanel activity={activity} onClose={() => setShowActivity(false)} />}
      </div>

      {board && openCard && (
        <CardModal
          key={openCard.id}
          card={openCard}
          columnName={board.columns.find((c) => c.id === openCard.column_id)?.name ?? ""}
          members={board.members}
          otherEditors={presence.filter((p) => p.id !== me.id && p.editing_card_id === openCard.id)}
          dispatch={dispatch}
          onClose={() => setOpenCardId(null)}
        />
      )}

      {board && showMembers && (
        <MembersDialog board={board} me={me} presence={presence} dispatch={dispatch} onClose={() => setShowMembers(false)} />
      )}

      {pendingDelete && (
        <Modal label="Supprimer la colonne" onClose={() => setPendingDelete(null)}>
          <div className="modal-head">
            <h2 style={{ fontSize: 17, fontWeight: 600, flex: 1 }}>Supprimer « {pendingDelete.name} » ?</h2>
          </div>
          <div className="modal-body">
            <p style={{ color: "var(--text-2)" }}>
              Ses {pendingDelete.cards.length} carte{pendingDelete.cards.length > 1 ? "s" : ""} seront supprimées pour tous les
              membres. Cette action est irréversible.
            </p>
          </div>
          <div className="modal-foot" style={{ justifyContent: "flex-end" }}>
            <button className="btn btn-ghost" onClick={() => setPendingDelete(null)}>Annuler</button>
            <button className="btn btn-dark"
              onClick={() => deleteColumn(pendingDelete)} autoFocus>
              Supprimer la colonne
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

function BoardTitle({ name, onRename }: { name: string; onRename: (name: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(name);

  const finish = (save: boolean) => {
    if (!editing) return;
    setEditing(false);
    const v = value.trim();
    if (save && v && v !== name) onRename(v);
  };

  if (!editing) {
    return (
      <button
        className="board-title"
        onClick={() => {
          setValue(name); // repart du nom à jour, qu'un autre membre a pu changer
          setEditing(true);
        }}
        title="Renommer le tableau"
      >
        {name}
      </button>
    );
  }
  return (
    <input
      className="board-title"
      autoFocus
      maxLength={120}
      value={value}
      aria-label="Nom du tableau"
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
        if (e.key === "Escape") finish(false);
      }}
    />
  );
}

function AddColumn({ boardId, onCreated }: { boardId: number; onCreated: (col: Column) => void }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      onCreated(await api.createColumn(boardId, name.trim()));
      setName("");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button className="btn add-column" onClick={() => setOpen(true)}>
        <Icon name="plus" size={15} /> Ajouter une colonne
      </button>
    );
  }
  return (
    <form className="add-column-form" onSubmit={submit}>
      <input className="input" autoFocus maxLength={60} placeholder="Nom de la colonne" value={name}
        aria-label="Nom de la nouvelle colonne"
        onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Escape" && setOpen(false)} />
      <div style={{ display: "flex", gap: 6 }}>
        <button className="btn btn-primary" disabled={busy || !name.trim()}>Ajouter</button>
        <button type="button" className="btn btn-ghost" onClick={() => setOpen(false)}>Annuler</button>
      </div>
    </form>
  );
}
