export interface User {
  id: number;
  name: string;
  email: string;
  color: string;
}

export interface Member extends User {
  role: "owner" | "member";
}

export interface Card {
  id: number;
  column_id: number;
  title: string;
  description: string;
  position: number;
  assignee_id: number | null;
  version: number;
  created_by_id: number;
  updated_at: string | null;
  /** Carte créée localement, pas encore confirmée par le serveur */
  pending?: boolean;
}

export interface Column {
  id: number;
  name: string;
  position: number;
  cards: Card[];
}

export interface Board {
  id: number;
  name: string;
  revision: number;
  role: "owner" | "member";
  members: Member[];
  columns: Column[];
}

export interface BoardSummary {
  id: number;
  name: string;
  role: "owner" | "member";
  created_at: string;
  card_count: number;
  members: Member[];
}

export interface Activity {
  id: number;
  message: string;
  created_at: string;
  user: Pick<User, "id" | "name" | "color">;
}

export interface PresenceUser {
  id: number;
  name: string;
  color: string;
  editing_card_id: number | null;
}

export type BoardEvent =
  | { type: "card.created"; payload: Card }
  | { type: "card.updated"; payload: Card }
  | { type: "card.moved"; payload: { card: Card; rebalanced: { id: number; position: number }[] } }
  | { type: "card.deleted"; payload: { id: number } }
  | { type: "column.created"; payload: Column }
  | { type: "column.updated"; payload: Omit<Column, "cards"> }
  | { type: "column.deleted"; payload: { id: number } }
  | { type: "columns.reordered"; payload: { columns: { id: number; position: number }[] } }
  | { type: "board.updated"; payload: { name: string } }
  | { type: "member.added"; payload: Member }
  | { type: "member.removed"; payload: { user_id: number; cards: Card[] } };

export type ServerEvent = BoardEvent & { revision: number; actor_id: number; activity?: Activity };

export type SocketMessage =
  | ServerEvent
  | { type: "presence"; users: PresenceUser[] }
  | { type: "board.deleted"; actor_id: number }
  | { type: "pong" };

export type ConnectionStatus = "connecting" | "live" | "reconnecting" | "offline";
