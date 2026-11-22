import { describe, expect, it } from "vitest";
import { boardReducer, findCard, type BoardState } from "./boardState";
import type { Board, Card, ServerEvent } from "./types";

const card = (id: number, columnId: number, position: number, extra: Partial<Card> = {}): Card => ({
  id,
  column_id: columnId,
  title: `Carte ${id}`,
  description: "",
  position,
  assignee_id: null,
  version: 1,
  created_by_id: 1,
  updated_at: null,
  ...extra,
});

const alice = { id: 1, name: "Alice", email: "alice@exemple.fr", color: "#16a34a", role: "owner" as const };
const bob = { id: 2, name: "Bob", email: "bob@exemple.fr", color: "#0ea5e9", role: "member" as const };

function makeBoard(): Board {
  return {
    id: 1,
    name: "Sprint",
    revision: 10,
    role: "owner",
    members: [alice, bob],
    columns: [
      { id: 1, name: "À faire", position: 1024, cards: [card(1, 1, 1024), card(2, 1, 2048)] },
      { id: 2, name: "En cours", position: 2048, cards: [card(3, 2, 1024, { assignee_id: 2 })] },
    ],
  };
}

const loaded = (): BoardState => boardReducer({ board: null }, { type: "snapshot", board: makeBoard() });
const ids = (state: BoardState, columnId: number) =>
  state.board!.columns.find((c) => c.id === columnId)!.cards.map((c) => c.id);
const server = (state: BoardState, event: Omit<ServerEvent, "revision" | "actor_id">, revision = state.board!.revision + 1) =>
  boardReducer(state, { type: "server", event: { ...event, revision, actor_id: 2 } as ServerEvent });

describe("événements du serveur", () => {
  it("applique un événement et avance la révision", () => {
    const next = server(loaded(), { type: "card.created", payload: card(4, 2, 2048) });
    expect(ids(next, 2)).toEqual([3, 4]);
    expect(next.board!.revision).toBe(11);
  });

  it("ignore un événement en double ou hors séquence", () => {
    const state = loaded();
    const duplicate = server(state, { type: "card.deleted", payload: { id: 1 } }, 10);
    const gap = server(state, { type: "card.deleted", payload: { id: 1 } }, 12);
    expect(duplicate).toBe(state);
    expect(gap).toBe(state);
  });

  it("est idempotent : l'écho d'une carte déjà créée localement ne la duplique pas", () => {
    let state = boardReducer(loaded(), { type: "card/upsert", card: card(4, 1, 3072) });
    state = server(state, { type: "card.created", payload: card(4, 1, 3072) });
    expect(ids(state, 1)).toEqual([1, 2, 4]);
  });

  it("déplace une carte et applique le rééquilibrage des positions", () => {
    const next = server(loaded(), {
      type: "card.moved",
      payload: {
        card: card(3, 1, 1500),
        rebalanced: [
          { id: 1, position: 2000 },
          { id: 2, position: 1000 },
        ],
      },
    });
    expect(ids(next, 1)).toEqual([2, 3, 1]);
    expect(ids(next, 2)).toEqual([]);
  });

  it("départage deux positions égales par id, comme le serveur", () => {
    const next = server(loaded(), { type: "card.updated", payload: card(2, 1, 1024) });
    expect(ids(next, 1)).toEqual([1, 2]);
  });

  it("une carte d'une colonne supprimée entre-temps n'est pas réinsérée", () => {
    let state = server(loaded(), { type: "column.deleted", payload: { id: 2 } });
    state = server(state, { type: "card.updated", payload: card(3, 2, 1024) });
    expect(state.board!.columns.map((c) => c.id)).toEqual([1]);
    expect(findCard(state.board, 3)).toBeUndefined();
  });

  it("réordonne les colonnes", () => {
    const next = server(loaded(), {
      type: "columns.reordered",
      payload: { columns: [{ id: 1, position: 4096 }] },
    });
    expect(next.board!.columns.map((c) => c.id)).toEqual([2, 1]);
  });

  it("garde les cartes d'une colonne renommée", () => {
    const next = server(loaded(), { type: "column.updated", payload: { id: 1, name: "Backlog", position: 1024 } });
    expect(next.board!.columns[0].name).toBe("Backlog");
    expect(ids(next, 1)).toEqual([1, 2]);
  });

  it("retire un membre et libère ses cartes", () => {
    const next = server(loaded(), {
      type: "member.removed",
      payload: { user_id: 2, cards: [card(3, 2, 1024, { assignee_id: null, version: 2 })] },
    });
    expect(next.board!.members.map((m) => m.id)).toEqual([1]);
    expect(findCard(next.board, 3)!.assignee_id).toBeNull();
  });
});

describe("actions locales (optimistes)", () => {
  it("remplace la carte provisoire par la carte confirmée", () => {
    let state = boardReducer(loaded(), { type: "card/upsert", card: card(-1, 1, 3072, { pending: true }) });
    state = boardReducer(state, { type: "card/upsert", card: card(4, 1, 3072), replaceId: -1 });
    expect(ids(state, 1)).toEqual([1, 2, 4]);
  });

  it("place une carte déplacée entre ses voisines", () => {
    const next = boardReducer(loaded(), { type: "card/move", id: 3, columnId: 1, index: 1 });
    expect(ids(next, 1)).toEqual([1, 3, 2]);
    const moved = findCard(next.board, 3)!;
    expect(moved.column_id).toBe(1);
    expect(moved.position).toBe(1536);
  });

  it("borne l'index d'arrivée et place la carte en fin de colonne", () => {
    const next = boardReducer(loaded(), { type: "card/move", id: 1, columnId: 2, index: 99 });
    expect(ids(next, 2)).toEqual([3, 1]);
    expect(findCard(next.board, 1)!.position).toBe(1024 + 1024);
  });

  it("ne fait rien sans tableau chargé", () => {
    const empty: BoardState = { board: null };
    expect(boardReducer(empty, { type: "card/remove", id: 1 })).toBe(empty);
  });
});
