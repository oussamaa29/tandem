import { useState, type Dispatch, type FormEvent } from "react";
import { api } from "../api";
import type { BoardAction } from "../boardState";
import { navigate } from "../router";
import type { Board, PresenceUser, User } from "../types";
import { Icon, Modal } from "./ui";
import { useToast } from "../toast";

interface Props {
  board: Board;
  me: User;
  presence: PresenceUser[];
  dispatch: Dispatch<BoardAction>;
  onClose: () => void;
}

export default function MembersDialog({ board, me, presence, dispatch, onClose }: Props) {
  const toast = useToast();
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isOwner = board.role === "owner";
  const online = new Set(presence.map((p) => p.id));

  const invite = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const member = await api.addMember(board.id, email.trim());
      toast(`${member.name} a rejoint le tableau`);
      setEmail("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (userId: number, name: string) => {
    try {
      await api.removeMember(board.id, userId);
      if (userId === me.id) {
        toast("Tu as quitté le tableau");
        navigate("/");
      } else {
        dispatch({ type: "member/remove", userId });
        toast(`${name} a été retiré du tableau`);
      }
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const deleteBoard = async () => {
    try {
      await api.deleteBoard(board.id);
      toast("Tableau supprimé");
      navigate("/");
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  return (
    <Modal onClose={onClose} label="Membres du tableau">
      <div className="modal-head">
        <div style={{ flex: 1 }}>
          <h2 style={{ fontSize: 17, fontWeight: 600 }}>Membres</h2>
          <p style={{ color: "var(--text-2)", marginTop: 2 }}>
            Tout le monde ici voit et modifie le tableau en temps réel.
          </p>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Fermer">
          <Icon name="x" />
        </button>
      </div>

      <div className="modal-body">
        {isOwner && (
          <form className="invite-form" onSubmit={invite}>
            <input className="input" type="email" required placeholder="E-mail de la personne à inviter" value={email}
              onChange={(e) => setEmail(e.target.value)} aria-label="E-mail à inviter" />
            <button className="btn btn-primary" disabled={busy || !email.trim()} style={{ height: 36 }}>
              Inviter
            </button>
          </form>
        )}
        {error && <div className="alert" role="alert">{error}</div>}
        {isOwner && !error && (
          <p className="hint" style={{ textAlign: "left", marginTop: -8 }}>
            La personne doit déjà avoir un compte Tandem.
          </p>
        )}

        <div>
          {board.members.map((m) => (
            <div key={m.id} className="member-row">
              <span className={online.has(m.id) ? "online-dot" : "offline-dot"} aria-label={online.has(m.id) ? "En ligne" : "Hors ligne"} />
              <div className="who">
                <div style={{ fontWeight: 500 }}>
                  {m.name} {m.id === me.id && <span style={{ color: "var(--text-3)", fontWeight: 400 }}>(toi)</span>}
                </div>
                <div>{online.has(m.id) ? "En ligne · " : ""}{m.email}</div>
              </div>
              {m.role === "owner" ? (
                <span className="badge badge-accent">Propriétaire</span>
              ) : m.id === me.id ? (
                <button className="btn btn-ghost btn-danger" onClick={() => remove(m.id, m.name)}>Quitter</button>
              ) : isOwner ? (
                <button className="btn btn-ghost" onClick={() => remove(m.id, m.name)} aria-label={`Retirer ${m.name}`}>
                  Retirer
                </button>
              ) : (
                <span className="badge">Membre</span>
              )}
            </div>
          ))}
        </div>
      </div>

      {isOwner && (
        <div className="modal-foot">
          {confirmDelete ? (
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ fontSize: 13 }}>Supprimer « {board.name} » et toutes ses cartes pour tout le monde ?</span>
              <button className="btn btn-danger" onClick={deleteBoard} autoFocus>Supprimer</button>
              <button className="btn btn-ghost" onClick={() => setConfirmDelete(false)}>Annuler</button>
            </div>
          ) : (
            <button className="btn btn-ghost btn-danger" onClick={() => setConfirmDelete(true)}>
              <Icon name="trash" size={15} /> Supprimer le tableau
            </button>
          )}
        </div>
      )}
    </Modal>
  );
}
