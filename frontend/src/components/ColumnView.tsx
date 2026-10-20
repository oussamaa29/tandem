import { useDroppable } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { cardDndId, columnDndId } from "../dnd";
import type { Column, Member, PresenceUser } from "../types";
import CardItem from "./CardItem";
import { Icon, Menu } from "./ui";

interface Props {
  column: Column;
  index: number;
  columnCount: number;
  members: Map<number, Member>;
  editors: Map<number, PresenceUser>;
  flashIds: Set<number>;
  isDropTarget: boolean;
  onOpenCard: (id: number) => void;
  onAddCard: (columnId: number, title: string) => void;
  onRename: (columnId: number, name: string) => void;
  onMove: (columnId: number, index: number) => void;
  onDelete: (column: Column) => void;
}

export default function ColumnView(props: Props) {
  const { column, index, columnCount, members, editors, flashIds, isDropTarget } = props;
  const { setNodeRef } = useDroppable({ id: columnDndId(column.id), data: { type: "column", columnId: column.id } });
  const [renaming, setRenaming] = useState(false);
  const [composing, setComposing] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  const isEmpty = column.cards.length === 0;

  return (
    <section className="column" aria-label={`Colonne ${column.name}`}>
      <header className="column-head">
        {renaming ? (
          <RenameInput
            initial={column.name}
            onDone={(name) => {
              setRenaming(false);
              if (name && name !== column.name) props.onRename(column.id, name);
            }}
          />
        ) : (
          <h2 className="column-name" onDoubleClick={() => setRenaming(true)} title="Double-clic pour renommer">
            {column.name}
          </h2>
        )}
        <span className="column-count" aria-label={`${column.cards.length} cartes`}>
          {column.cards.length}
        </span>
        <Menu
          label={`Actions de la colonne ${column.name}`}
          items={[
            { label: "Ajouter une carte", icon: "plus", onSelect: () => setComposing(true) },
            { label: "Renommer", icon: "edit", onSelect: () => setRenaming(true) },
            { label: "Déplacer à gauche", icon: "arrowLeft", disabled: index === 0, onSelect: () => props.onMove(column.id, index - 1), separatorBefore: true },
            { label: "Déplacer à droite", icon: "arrowRight", disabled: index === columnCount - 1, onSelect: () => props.onMove(column.id, index + 1) },
            { label: "Supprimer la colonne", icon: "trash", danger: true, separatorBefore: true, onSelect: () => props.onDelete(column) },
          ]}
        />
      </header>

      <SortableContext items={column.cards.map((c) => cardDndId(c.id))} strategy={verticalListSortingStrategy}>
        <div
          ref={(node) => {
            setNodeRef(node);
            listRef.current = node;
          }}
          className={`card-list ${isEmpty ? "empty" : ""} ${isDropTarget ? "drop-target" : ""}`}
        >
          {column.cards.map((card) => (
            <CardItem
              key={card.id}
              card={card}
              assignee={card.assignee_id ? members.get(card.assignee_id) : undefined}
              editor={editors.get(card.id)}
              flash={flashIds.has(card.id)}
              onOpen={props.onOpenCard}
            />
          ))}
        </div>
      </SortableContext>

      <div className="column-foot">
        {composing ? (
          <Composer
            onSubmit={(title) => {
              props.onAddCard(column.id, title);
              // Garde le focus : on enchaîne les saisies, puis la liste défile vers la nouvelle carte
              requestAnimationFrame(() => listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" }));
            }}
            onClose={() => setComposing(false)}
          />
        ) : (
          <button className="btn btn-ghost add-card" onClick={() => setComposing(true)}>
            <Icon name="plus" size={15} /> Ajouter une carte
          </button>
        )}
      </div>
    </section>
  );
}

function RenameInput({ initial, onDone }: { initial: string; onDone: (value: string | null) => void }) {
  const [value, setValue] = useState(initial);
  const done = useRef(false);
  const finish = (v: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(v);
  };
  return (
    <input
      className="input column-name-input"
      autoFocus
      maxLength={60}
      value={value}
      aria-label="Nom de la colonne"
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => finish(value.trim() || null)}
      onKeyDown={(e) => {
        if (e.key === "Enter") finish(value.trim() || null);
        if (e.key === "Escape") finish(null);
      }}
    />
  );
}

function Composer({ onSubmit, onClose }: { onSubmit: (title: string) => void; onClose: () => void }) {
  const [title, setTitle] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => ref.current?.focus(), []);

  const submit = () => {
    const t = title.trim();
    if (!t) return;
    onSubmit(t);
    setTitle("");
    ref.current?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
    if (e.key === "Escape") onClose();
  };

  return (
    <div className="composer">
      <textarea
        ref={ref}
        value={title}
        maxLength={200}
        placeholder="Titre de la carte…"
        aria-label="Titre de la nouvelle carte"
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => !title.trim() && onClose()}
      />
      <div className="row">
        <button className="btn btn-primary" onMouseDown={(e) => e.preventDefault()} onClick={submit} disabled={!title.trim()}>
          Ajouter
        </button>
        <button className="btn btn-ghost" onClick={onClose} aria-label="Fermer">
          <Icon name="x" size={15} />
        </button>
        <span className="hint hide-mobile" style={{ marginLeft: "auto" }}>
          <kbd>Entrée</kbd> pour ajouter
        </span>
      </div>
    </div>
  );
}
