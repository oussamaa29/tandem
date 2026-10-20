/**
 * État d'un tableau côté client.
 *
 * Deux sources de modifications convergent ici :
 *  - les actions locales « optimistes » (l'écran réagit avant la réponse du serveur) ;
 *  - les événements temps réel diffusés par le serveur, y compris ceux de nos propres actions.
 *
 * Tous les handlers sont des « upserts » idempotents : appliquer deux fois le même événement,
 * ou recevoir l'écho d'une action déjà appliquée localement, ne change rien. Les cartes d'une
 * colonne sont toujours triées par position, ce qui rend l'ordre déterministe.
 */
import type { Board, Card, Column, Member, ServerEvent } from "./types";

export interface BoardState {
  board: Board | null;
}

export type BoardAction =
  | { type: "snapshot"; board: Board }
  | { type: "server"; event: ServerEvent }
  | { type: "card/upsert"; card: Card; replaceId?: number }
  | { type: "card/remove"; id: number }
  | { type: "card/move"; id: number; columnId: number; index: number }
  | { type: "column/upsert"; column: Omit<Column, "cards"> & { cards?: Card[] } }
  | { type: "column/remove"; id: number }
  | { type: "board/rename"; name: string }
  | { type: "member/remove"; userId: number };

export const initialBoardState: BoardState = { board: null };

// Départage par id, comme le serveur : deux positions égales (déplacements simultanés) gardent
// le même ordre chez tout le monde
const byPosition = <T extends { position: number; id: number }>(a: T, b: T) => a.position - b.position || a.id - b.id;

function withoutCard(columns: Column[], id: number): Column[] {
  return columns.map((col) =>
    col.cards.some((c) => c.id === id) ? { ...col, cards: col.cards.filter((c) => c.id !== id) } : col,
  );
}

function upsertCard(columns: Column[], card: Card, replaceId?: number): Column[] {
  let next = withoutCard(columns, card.id);
  if (replaceId !== undefined) next = withoutCard(next, replaceId);
  if (!next.some((col) => col.id === card.column_id)) return next; // colonne supprimée entre-temps
  return next.map((col) => (col.id === card.column_id ? { ...col, cards: [...col.cards, card].sort(byPosition) } : col));
}

/** Position provisoire pour un déplacement local ; le serveur renverra la valeur définitive. */
function provisionalPosition(cards: Card[], index: number): number {
  const before = cards[index - 1]?.position;
  const after = cards[index]?.position;
  if (before === undefined && after === undefined) return 1024;
  if (before === undefined) return after! / 2;
  if (after === undefined) return before + 1024;
  return (before + after) / 2;
}

function moveCard(columns: Column[], id: number, columnId: number, index: number): Column[] {
  const card = columns.flatMap((c) => c.cards).find((c) => c.id === id);
  const target = columns.find((c) => c.id === columnId);
  if (!card || !target) return columns;
  const siblings = target.cards.filter((c) => c.id !== id);
  const clamped = Math.max(0, Math.min(index, siblings.length));
  const moved = { ...card, column_id: columnId, position: provisionalPosition(siblings, clamped) };
  return upsertCard(columns, moved);
}

function applyServerEvent(board: Board, event: ServerEvent): Board {
  switch (event.type) {
    case "card.created":
    case "card.updated":
      return { ...board, columns: upsertCard(board.columns, event.payload) };
    case "card.moved": {
      let columns = upsertCard(board.columns, event.payload.card);
      if (event.payload.rebalanced.length) {
        const positions = new Map(event.payload.rebalanced.map((r) => [r.id, r.position]));
        columns = columns.map((col) => ({
          ...col,
          cards: col.cards
            .map((c) => (positions.has(c.id) ? { ...c, position: positions.get(c.id)! } : c))
            .sort(byPosition),
        }));
      }
      return { ...board, columns };
    }
    case "card.deleted":
      return { ...board, columns: withoutCard(board.columns, event.payload.id) };
    case "column.created":
    case "column.updated":
      return { ...board, columns: upsertColumn(board.columns, event.payload) };
    case "column.deleted":
      return { ...board, columns: board.columns.filter((c) => c.id !== event.payload.id) };
    case "columns.reordered": {
      const positions = new Map(event.payload.columns.map((c) => [c.id, c.position]));
      return {
        ...board,
        columns: board.columns
          .map((c) => (positions.has(c.id) ? { ...c, position: positions.get(c.id)! } : c))
          .sort(byPosition),
      };
    }
    case "board.updated":
      return { ...board, name: event.payload.name };
    case "member.added":
      return { ...board, members: upsertMember(board.members, event.payload) };
    case "member.removed": {
      let columns = board.columns;
      for (const card of event.payload.cards) columns = upsertCard(columns, card);
      return { ...board, columns, members: board.members.filter((m) => m.id !== event.payload.user_id) };
    }
  }
}

function upsertColumn(columns: Column[], column: Omit<Column, "cards"> & { cards?: Card[] }): Column[] {
  const existing = columns.find((c) => c.id === column.id);
  const merged: Column = { ...column, cards: column.cards ?? existing?.cards ?? [] };
  return [...columns.filter((c) => c.id !== column.id), merged].sort(byPosition);
}

function upsertMember(members: Member[], member: Member): Member[] {
  return [...members.filter((m) => m.id !== member.id), member];
}

export function boardReducer(state: BoardState, action: BoardAction): BoardState {
  if (action.type === "snapshot") return { board: action.board };
  const board = state.board;
  if (!board) return state;

  switch (action.type) {
    case "server": {
      // L'ordre des révisions est vérifié en amont (useBoard) ; garde-fou si un événement arrive en double
      const { event } = action;
      if (event.revision !== board.revision + 1) return state;
      return { board: { ...applyServerEvent(board, event), revision: event.revision } };
    }
    case "card/upsert":
      return { ...state, board: { ...board, columns: upsertCard(board.columns, action.card, action.replaceId) } };
    case "card/remove":
      return { ...state, board: { ...board, columns: withoutCard(board.columns, action.id) } };
    case "card/move":
      return { ...state, board: { ...board, columns: moveCard(board.columns, action.id, action.columnId, action.index) } };
    case "column/upsert":
      return { ...state, board: { ...board, columns: upsertColumn(board.columns, action.column) } };
    case "column/remove":
      return { ...state, board: { ...board, columns: board.columns.filter((c) => c.id !== action.id) } };
    case "board/rename":
      return { ...state, board: { ...board, name: action.name } };
    case "member/remove":
      return { ...state, board: { ...board, members: board.members.filter((m) => m.id !== action.userId) } };
  }
}

export const findCard = (board: Board | null, id: number | null): Card | undefined =>
  id === null || !board ? undefined : board.columns.flatMap((c) => c.cards).find((c) => c.id === id);
