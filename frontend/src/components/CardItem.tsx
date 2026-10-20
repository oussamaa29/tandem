import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { CSSProperties } from "react";
import { cardDndId } from "../dnd";
import type { Card, Member, PresenceUser } from "../types";
import { Icon, Initials } from "./ui";

interface Props {
  card: Card;
  assignee?: Member;
  editor?: PresenceUser;
  flash?: boolean;
  onOpen: (id: number) => void;
}

export function CardBody({ card, assignee, editor }: Pick<Props, "card" | "assignee" | "editor">) {
  const hasDescription = card.description.trim().length > 0;
  return (
    <>
      {editor && <span className="editing-tag">{editor.name.split(" ")[0]} modifie…</span>}
      <div className="card-title">{card.title}</div>
      {(hasDescription || assignee) && (
        <div className="card-foot">
          {hasDescription && (
            <span title="Contient une description" aria-label="Contient une description">
              <Icon name="text" size={14} />
            </span>
          )}
          <span className="grow" />
          {assignee && <Initials name={assignee.name} title={`Assignée à ${assignee.name}`} />}
        </div>
      )}
    </>
  );
}

export default function CardItem({ card, assignee, editor, flash, onOpen }: Props) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: cardDndId(card.id),
    disabled: card.pending,
    data: { type: "card", columnId: card.column_id },
  });

  const style: CSSProperties = {
    transform: CSS.Translate.toString(transform),
    transition,
  };

  const classes = [
    "card",
    isDragging && "is-dragging",
    card.pending && "is-pending",
    flash && !isDragging && "is-flash",
    editor && "is-edited",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button
      ref={setNodeRef}
      type="button"
      style={style}
      className={classes}
      {...attributes}
      {...listeners}
      aria-roledescription="carte déplaçable"
      aria-label={`${card.title}${assignee ? `, assignée à ${assignee.name}` : ""}. Entrée pour ouvrir, Espace pour déplacer.`}
      onClick={() => !card.pending && onOpen(card.id)}
      onKeyDown={(e) => {
        // Entrée ouvre la carte ; Espace reste réservé au glisser-déposer clavier de dnd-kit.
        // Pendant un glisser au clavier, Entrée sert à déposer : elle ne doit pas aussi ouvrir la carte.
        if (e.key === "Enter" && !card.pending && !isDragging) {
          e.preventDefault();
          onOpen(card.id);
          return;
        }
        listeners?.onKeyDown?.(e);
      }}
    >
      <CardBody card={card} assignee={assignee} editor={editor} />
    </button>
  );
}
