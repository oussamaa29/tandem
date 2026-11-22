import { describe, expect, it } from "vitest";
import { parseCardId, parseColumnId, cardDndId, columnDndId } from "./dnd";
import { initials, parseServerDate, plural, timeAgo } from "./format";

describe("dates du serveur", () => {
  it("lit une date sans fuseau comme de l'UTC", () => {
    expect(parseServerDate("2026-11-20T10:00:00").toISOString()).toBe("2026-11-20T10:00:00.000Z");
    expect(parseServerDate("2026-11-20T10:00:00+02:00").toISOString()).toBe("2026-11-20T08:00:00.000Z");
  });

  it("formate une durée relative", () => {
    const now = Date.parse("2026-11-20T10:00:00Z");
    expect(timeAgo("2026-11-20T09:59:50", now)).toBe("à l'instant");
    expect(timeAgo("2026-11-20T09:55:00", now)).toBe("il y a 5 minutes");
    expect(timeAgo("2026-11-20T07:00:00", now)).toBe("il y a 3 heures");
  });
});

describe("utilitaires", () => {
  it("initiales et pluriel", () => {
    expect(initials("Sarah  Martin Dupont")).toBe("SM");
    expect(plural(1, "carte", "cartes")).toBe("1 carte");
    expect(plural(3, "carte", "cartes")).toBe("3 cartes");
  });

  it("identifiants dnd-kit aller-retour, y compris les cartes provisoires (id négatif)", () => {
    expect(parseCardId(cardDndId(42))).toBe(42);
    expect(parseCardId(cardDndId(-1))).toBe(-1);
    expect(parseColumnId(columnDndId(7))).toBe(7);
    expect(parseCardId(columnDndId(7))).toBeNull();
    expect(parseColumnId("card-3")).toBeNull();
  });
});
