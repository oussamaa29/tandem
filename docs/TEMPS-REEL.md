# Temps réel sur plusieurs instances (Redis)

Code : `backend/hub.py`, tests : `backend/tests/test_hub_redis.py`.

```
            ┌──────────── répartiteur de charge (WebSocket activé) ────────────┐
            ▼                                                                  ▼
   instance A (uvicorn)                                              instance B (uvicorn)
   connexions WS locales  ──PUBLISH tandem:board:12──▶  Redis  ──▶  abonnée à tandem:board:*
   présence de A ─HSET tandem:presence:12 A─▶ (hash par tableau)  ◀─HSET … B─ présence de B
```

```bash
# même base Postgres et même JWT_SECRET pour toutes les instances
REDIS_URL=redis://redis:6379/0 uvicorn main:app --port 8041
REDIS_URL=redis://redis:6379/0 uvicorn main:app --port 8042   # ou --workers N : chaque processus est une instance
```

Aucune affinité de session (« sticky sessions ») n'est nécessaire : REST et WebSocket d'un même utilisateur peuvent arriver sur des instances différentes.

| | Portée |
|---|---|
| Événements (`card.moved`…), retrait d'un membre (fermeture 4403), suppression du tableau (4404) | **Globale** : publiés sur `tandem:board:<id>`, chaque instance les remet à ses connexions. L'instance qui publie reçoit son propre message par l'abonnement, comme les autres (pas de double envoi), et l'ordre des événements d'un même émetteur est conservé |
| Présence et « Sarah modifie… » | **Globale** : chaque instance écrit ses utilisateurs dans `tandem:presence:<id>` (un champ par instance) ; toute modification déclenche la diffusion de la liste fusionnée, dédoublonnée par utilisateur (plusieurs onglets ou instances) |
| Connexions WebSocket, file d'envoi et tâche d'écriture par connexion | **Locale** à l'instance |

Pannes :

- **Client lent ou bloqué** : diffuser ne fait que mettre en file (une file et une tâche d'écriture par connexion, ordre conservé), donc un client qui ne lit plus ne retarde ni les autres membres ni les autres tableaux. Si sa file déborde (256 messages) ou qu'un envoi dépasse 10 s, sa connexion est fermée (code 4408) : le client se reconnecte et recharge le tableau. Valable aussi sans Redis.
- **Instance arrêtée brutalement** : sa clé de vie (TTL 15 s, rafraîchie toutes les 5 s) expire ; une autre instance la retire alors de la présence et diffuse la liste à jour. Un arrêt normal la retire immédiatement.
- **Redis indisponible, ou abonnement d'une seule instance coupé** : l'erreur est journalisée, chaque instance continue de servir ses propres connexions (diffusion locale uniquement) et l'abonnement se reconnecte avec un délai exponentiel (0,5 s → 10 s). Ce qui a été publié ailleurs pendant la coupure est perdu pour cette instance :
  - les **événements** sont rattrapés par les clients, qui détectent le trou dans les révisions et rechargent le tableau ;
  - les **retraits de membre et suppressions de tableau** ne laissent pas de trou de révision : à chaque réabonnement, avant de remettre le moindre message, l'instance revérifie donc en base les droits de toutes ses connexions et ferme celles des membres retirés (4403) et des tableaux supprimés (4404).
