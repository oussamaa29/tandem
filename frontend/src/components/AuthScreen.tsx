import { useState, type FormEvent } from "react";
import { api } from "../api";
import type { User } from "../types";
import { Logo } from "./ui";

type Mode = "login" | "register";

export default function AuthScreen({ onAuth }: { onAuth: (token: string, user: User) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = mode === "login" ? await api.login(email, password) : await api.register(name, email, password);
      onAuth(res.token, res.user);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  const switchMode = (m: Mode) => {
    setMode(m);
    setError(null);
  };

  return (
    <div className="auth">
      <aside className="auth-aside">
        <Logo />
        <h2>
          Un tableau.
          <br />
          Plusieurs personnes.
          <br />
          <span className="accent-text">Le même état, partout.</span>
        </h2>
        <ul className="auth-facts">
          <li>Chaque déplacement apparaît chez les autres sans recharger.</li>
          <li>Deux modifications de la même carte ne s'écrasent jamais.</li>
          <li>Une coupure réseau se rattrape toute seule.</li>
        </ul>
      </aside>

      <main className="auth-main">
        <form className="auth-form" onSubmit={submit}>
          <div>
            <h1>{mode === "login" ? "Connexion" : "Créer un compte"}</h1>
            <p className="sub">
              {mode === "login" ? "Retrouve tes tableaux." : "Un nom, un e-mail, un mot de passe."}
            </p>
          </div>

          <div className="segmented" role="group" aria-label="Mode">
            <button type="button" aria-pressed={mode === "login"} onClick={() => switchMode("login")}>
              Connexion
            </button>
            <button type="button" aria-pressed={mode === "register"} onClick={() => switchMode("register")}>
              Inscription
            </button>
          </div>

          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}

          {mode === "register" && (
            <label className="field">
              <span>Nom affiché</span>
              <input className="input" required maxLength={80} autoComplete="name" value={name}
                onChange={(e) => setName(e.target.value)} placeholder="Oussama" />
            </label>
          )}
          <label className="field">
            <span>E-mail</span>
            <input className="input" type="email" required autoComplete="email" value={email}
              onChange={(e) => setEmail(e.target.value)} placeholder="toi@exemple.com" />
          </label>
          <label className="field">
            <span>Mot de passe</span>
            <input className="input" type="password" required minLength={mode === "register" ? 8 : undefined}
              autoComplete={mode === "login" ? "current-password" : "new-password"} value={password}
              onChange={(e) => setPassword(e.target.value)} placeholder={mode === "register" ? "8 caractères minimum" : ""} />
          </label>

          <button className="btn btn-primary btn-lg btn-block" disabled={busy}>
            {busy ? "Un instant…" : mode === "login" ? "Se connecter" : "Créer mon compte"}
          </button>
          <p className="hint">Astuce : ouvre l'app dans deux navigateurs pour voir la synchronisation en direct.</p>
        </form>
      </main>
    </div>
  );
}
