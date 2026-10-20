import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";
import { api, session, socketUrl } from "./api";
import { boardReducer, initialBoardState } from "./boardState";
import type { Activity, ConnectionStatus, PresenceUser, ServerEvent, SocketMessage } from "./types";

const HEARTBEAT_MS = 25_000;
const MAX_BACKOFF_MS = 15_000;
/** Délai laissé à un événement manquant pour arriver avant de tout recharger (voir applyEvent) */
const GAP_WAIT_MS = 400;

export type BoardEnd = "deleted" | "removed" | "unauthorized" | "not-found";

interface Options {
  boardId: number;
  meId: number;
  /** Appelé pour chaque événement venant d'un autre membre (surbrillance, notifications…) */
  onRemoteEvent?: (event: ServerEvent) => void;
  onEnd: (reason: BoardEnd) => void;
}

/** Fusionne l'historique rechargé avec les entrées reçues en direct entre-temps (ids croissants). */
function mergeActivity(fetched: Activity[], live: Activity[]): Activity[] {
  const byId = new Map([...live, ...fetched].map((a) => [a.id, a]));
  return [...byId.values()].sort((a, b) => b.id - a.id).slice(0, 100);
}

/**
 * Charge un tableau et le garde synchronisé.
 *
 * Ordre volontaire : on ouvre d'abord le WebSocket, et ce n'est qu'une fois entré dans la room
 * (premier message de présence) qu'on charge l'état complet. Les événements reçus pendant le
 * chargement sont mis en tampon et rejoués ensuite — ceux déjà inclus dans l'état chargé sont
 * ignorés grâce au numéro de révision. Aucune fenêtre où une modification pourrait passer inaperçue.
 */
export function useBoard({ boardId, meId, onRemoteEvent, onEnd }: Options) {
  const [state, dispatch] = useReducer(boardReducer, initialBoardState);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [presence, setPresence] = useState<PresenceUser[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const bufferRef = useRef<ServerEvent[] | null>(null);
  const callbacks = useRef({ onRemoteEvent, onEnd });
  useLayoutEffect(() => {
    callbacks.current = { onRemoteEvent, onEnd };
  });

  // Un tableau ne se « termine » qu'une fois : WebSocket refusé et chargement en 404 peuvent
  // arriver tous les deux pour la même cause, sans double message ni double navigation
  const endedRef = useRef(false);
  const end = useCallback((reason: BoardEnd) => {
    if (endedRef.current) return;
    endedRef.current = true;
    callbacks.current.onEnd(reason);
  }, []);
  const loadedRef = useRef(false); // un état complet a-t-il déjà été chargé ?

  // Révision du dernier état appliqué, tenue à jour de façon synchrone : une rafale d'événements
  // reçus entre deux rendus est vérifiée correctement, sans attendre que React ait re-rendu.
  const revisionRef = useRef<number | null>(null);
  const resyncRef = useRef<() => void>(() => {});
  // Événements arrivés en avance (révision > attendue + 1), en attente du ou des manquants
  const pendingRef = useRef(new Map<number, ServerEvent>());
  const gapTimer = useRef<number | undefined>(undefined);
  // Chaque resynchronisation a un numéro : seule la plus récente applique son résultat
  const generation = useRef(0);

  const clearGap = useCallback(() => {
    window.clearTimeout(gapTimer.current);
    gapTimer.current = undefined;
  }, []);

  const applyEvent = useCallback(
    (event: ServerEvent) => {
      if (bufferRef.current) {
        bufferRef.current.push(event);
        return;
      }
      const current = revisionRef.current;
      if (current === null || event.revision <= current) return; // déjà inclus dans notre état
      if (event.revision > current + 1) {
        // Trou dans la séquence. Le serveur diffuse en arrière-plan : deux écritures quasi
        // simultanées peuvent partir dans le désordre. On garde l'événement de côté et on laisse
        // un court délai au manquant ; s'il n'arrive pas, rechargement complet.
        pendingRef.current.set(event.revision, event);
        if (gapTimer.current === undefined) {
          gapTimer.current = window.setTimeout(() => {
            gapTimer.current = undefined;
            resyncRef.current();
          }, GAP_WAIT_MS);
        }
        return;
      }
      revisionRef.current = event.revision;
      dispatch({ type: "server", event });

      // Le manquant est arrivé : on applique dans l'ordre ceux qui attendaient derrière lui
      const pending = pendingRef.current;
      let next = pending.get(revisionRef.current + 1);
      while (next) {
        pending.delete(next.revision);
        revisionRef.current = next.revision;
        dispatch({ type: "server", event: next });
        next = pending.get(revisionRef.current + 1);
      }
      for (const rev of pending.keys()) if (rev <= revisionRef.current) pending.delete(rev);
      if (pending.size === 0) clearGap();
    },
    [clearGap],
  );

  const resync = useCallback(async () => {
    const gen = ++generation.current;
    clearGap();
    // Les événements en attente rejoignent le tampon : ils seront rejoués après le chargement
    bufferRef.current = [...pendingRef.current.values()];
    pendingRef.current.clear();
    try {
      const [board, history] = await Promise.all([api.getBoard(boardId), api.activity(boardId).catch(() => null)]);
      if (gen !== generation.current) return; // une resynchronisation plus récente a pris le relais
      const buffered = (bufferRef.current ?? []).sort((a, b) => a.revision - b.revision);
      bufferRef.current = null;
      revisionRef.current = board.revision;
      loadedRef.current = true;
      dispatch({ type: "snapshot", board });
      setLoadError(null);
      // Activité manquée pendant une coupure : rechargée à chaque resynchronisation
      if (history) setActivity((live) => mergeActivity(history, live));
      // Un trou trouvé ici attend le manquant (applyEvent) au lieu de relancer une resynchronisation imbriquée
      for (const event of buffered) applyEvent(event);
    } catch (e) {
      if (gen !== generation.current) return;
      bufferRef.current = null;
      const status = (e as { status?: number }).status;
      if (status === 404) end("not-found");
      else setLoadError((e as Error).message);
    }
  }, [boardId, applyEvent, clearGap, end]);
  useLayoutEffect(() => {
    resyncRef.current = resync;
  }, [resync]);

  useEffect(() => {
    // Propre à cette exécution de l'effet : en StrictMode (ou au démontage), les événements tardifs
    // d'une connexion abandonnée ne doivent toucher ni l'état ni la connexion suivante
    let cancelled = false;
    let socket: WebSocket | null = null;
    let attempt = 0;
    let closedByUs = false;
    let retryTimer: number | undefined;
    let heartbeat: number | undefined;

    const connect = () => {
      const token = session.get();
      if (!token) return end("unauthorized");
      setStatus(attempt === 0 ? "connecting" : "reconnecting");
      const ws = new WebSocket(socketUrl(boardId));
      socket = ws;
      wsRef.current = ws;
      let joined = false;

      ws.onopen = () => {
        if (cancelled || socket !== ws) return;
        // Premier message : l'authentification (le jeton n'est jamais dans l'URL)
        ws.send(JSON.stringify({ type: "auth", token }));
        heartbeat = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
        }, HEARTBEAT_MS);
      };

      ws.onmessage = (msg) => {
        if (cancelled || socket !== ws) return;
        let data: SocketMessage;
        try {
          data = JSON.parse(msg.data);
        } catch {
          return;
        }
        if (data.type === "pong") return;
        if (data.type === "presence") {
          // La première présence confirme qu'on est dans la room : on peut charger l'état sans
          // risquer de rater un événement publié entre le chargement et l'abonnement
          if (!joined) {
            joined = true;
            attempt = 0;
            setStatus("live");
            resync();
          }
          return setPresence(data.users);
        }
        if (data.type === "board.deleted") {
          closedByUs = true;
          // Si c'est nous qui avons supprimé, l'écran a déjà navigué et affiché sa confirmation
          if (data.actor_id !== meId) end("deleted");
          return;
        }
        const event = data as ServerEvent;
        if (event.type === "member.removed" && event.payload.user_id === meId) {
          closedByUs = true;
          if (event.actor_id !== meId) end("removed");
          return;
        }
        if (event.activity) {
          const entry = event.activity;
          setActivity((list) => (list.some((a) => a.id === entry.id) ? list : [entry, ...list].slice(0, 100)));
        }
        applyEvent(event);
        if (event.actor_id !== meId) callbacks.current.onRemoteEvent?.(event);
      };

      ws.onclose = (e) => {
        if (cancelled || socket !== ws) return;
        window.clearInterval(heartbeat);
        socket = null;
        if (wsRef.current === ws) wsRef.current = null;
        setPresence([]);
        if (closedByUs) return;
        if (e.code === 4401) return end("unauthorized");
        // Refusé avant tout chargement : on n'a jamais eu accès à ce tableau (même message que le 404)
        if (e.code === 4403) return end(loadedRef.current ? "removed" : "not-found");
        if (e.code === 4404) return end("deleted");
        // Backoff exponentiel avec gigue, pour ne pas que tous les clients reviennent en même temps
        const delay = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** attempt) * (0.7 + Math.random() * 0.6);
        attempt += 1;
        setStatus(navigator.onLine ? "reconnecting" : "offline");
        retryTimer = window.setTimeout(connect, delay);
      };
    };

    // Retour du réseau : on n'attend pas la fin du backoff
    const reconnectNow = () => {
      if (cancelled || socket || closedByUs) return;
      window.clearTimeout(retryTimer);
      attempt = 0;
      connect();
    };
    const goOffline = () => setStatus((s) => (s === "live" ? s : "offline"));

    connect();
    window.addEventListener("online", reconnectNow);
    window.addEventListener("offline", goOffline);
    return () => {
      cancelled = true;
      closedByUs = true;
      window.clearTimeout(retryTimer);
      window.clearInterval(heartbeat);
      clearGap();
      window.removeEventListener("online", reconnectNow);
      window.removeEventListener("offline", goOffline);
      if (socket && wsRef.current === socket) wsRef.current = null;
      socket?.close(1000);
    };
  }, [boardId, meId, resync, applyEvent, clearGap, end]);

  const sendEditing = useCallback((cardId: number | null) => {
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "editing", card_id: cardId }));
  }, []);

  return { board: state.board, dispatch, status, presence, activity, loadError, resync, sendEditing };
}
