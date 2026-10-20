import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "../api";
import { plural } from "../format";
import { navigate } from "../router";
import type { BoardSummary, User } from "../types";
import { Icon } from "./ui";
import { useToast } from "../toast";

export default function BoardsHome({ me }: { me: User }) {
  const [boards, setBoards] = useState<BoardSummary[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.listBoards().then(setBoards).catch((e) => {
      setBoards([]);
      toast(e.message, "error");
    });
  }, [toast]);

  useEffect(() => {
    if (creating) inputRef.current?.focus();
  }, [creating]);

  const create = async (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const board = await api.createBoard(name.trim());
      navigate(`/b/${board.id}`);
    } catch (err) {
      toast((err as Error).message, "error");
      setBusy(false);
    }
  };

  const firstName = me.name.split(" ")[0];

  return (
    <main className="home">
      <div className="home-head">
        <div>
          <p className="eyebrow">{firstName}</p>
          <h1>Tableaux</h1>
          {boards !== null && boards.length > 0 && <p>{plural(boards.length, "tableau", "tableaux")}</p>}
        </div>
        {boards && boards.length > 0 && !creating && (
          <button className="btn btn-primary" onClick={() => setCreating(true)}>
            <Icon name="plus" size={15} /> Nouveau tableau
          </button>
        )}
      </div>

      {creating && (
        <form className="new-board" onSubmit={create}>
          <input ref={inputRef} className="input" placeholder="Nom du tableau" maxLength={120} value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setCreating(false)} />
          <button className="btn btn-primary" disabled={busy || !name.trim()}>Créer</button>
          <button type="button" className="btn btn-ghost" onClick={() => setCreating(false)}>Annuler</button>
        </form>
      )}

      {boards === null ? (
        <div className="board-list" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <div key={i} className="skeleton" style={{ height: 56, margin: "1px 0" }} />
          ))}
        </div>
      ) : boards.length === 0 && !creating ? (
        <div className="empty-state">
          <h2>Aucun tableau.</h2>
          <p>Crée un tableau, invite quelqu'un, et vos modifications apparaîtront chez l'autre en direct.</p>
          <button className="btn btn-primary btn-lg" onClick={() => setCreating(true)}>
            Créer un tableau
          </button>
        </div>
      ) : (
        <div className="board-list">
          <div className="board-row board-row-head" aria-hidden="true">
            <span>Nom</span>
            <span>Cartes</span>
            <span className="hide-mobile">Membres</span>
            <span className="hide-mobile">Rôle</span>
          </div>
          {boards.map((b) => (
            <a key={b.id} href={`#/b/${b.id}`} className="board-row">
              <span className="board-row-name">{b.name}</span>
              <span className="mono">{b.card_count}</span>
              <span className="hide-mobile board-row-members">
                {b.members.map((m) => (m.id === me.id ? "toi" : m.name.split(" ")[0])).join(", ")}
              </span>
              <span className="hide-mobile mono">{b.role === "owner" ? "propriétaire" : "membre"}</span>
            </a>
          ))}
        </div>
      )}
    </main>
  );
}
