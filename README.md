# Tandem — Kanban collaboratif en temps réel

[![CI](https://github.com/oussamaa29/tandem/actions/workflows/ci.yml/badge.svg)](https://github.com/oussamaa29/tandem/actions/workflows/ci.yml)

Tableaux Kanban partagés : quand un membre déplace une carte, la renomme ou l'assigne, tous les autres le voient **instantanément**, sans recharger. Présence en direct, indicateur « Sarah modifie… », historique d'activité, et gestion explicite des conflits d'édition.

## Pourquoi ce projet

Un CRUD affiche des données ; une app collaborative doit les garder **cohérentes entre plusieurs personnes qui agissent en même temps**. Ce projet traite les vrais problèmes de ce genre d'app :

| Problème | Solution retenue |
|---|---|
| Diffuser chaque modification à tous les membres | WebSocket par tableau (« room ») ; les écritures passent par l'API REST, puis l'événement est diffusé |
| Ne jamais rater une modification (coupure réseau, onglet en veille) | Chaque tableau a une **révision** incrémentée à chaque écriture. Un client qui voit un trou dans la séquence attend brièvement l'événement manquant (deux diffusions simultanées peuvent arriver dans le désordre), puis se resynchronise. Reconnexion automatique avec backoff exponentiel + gigue |
| Pas de fenêtre aveugle au chargement | Le client rejoint la room WebSocket **avant** de charger le tableau, met en tampon les événements reçus pendant le chargement, puis les rejoue |
| Deux personnes éditent la même carte | **Verrou optimiste** (`version`, vérifiée dans l'`UPDATE … WHERE version = :v` lui-même, donc correct même pour deux requêtes simultanées) : l'enregistrement périmé est refusé (409) et l'utilisateur choisit « voir sa version » ou « garder la mienne ». Jamais d'écrasement silencieux |
| Réordonner sans réécrire toute la colonne | **Indexation fractionnaire** : une carte insérée prend la moyenne des positions de ses voisines (1 ligne modifiée). Rééquilibrage automatique quand l'écart devient trop faible. Deux positions égales (déplacements simultanés) sont départagées par id, de façon identique côté serveur et client |
| Interface réactive malgré la latence | **UI optimiste** : la carte bouge tout de suite. En cas d'échec, l'état est restauré depuis le serveur |
| Membre retiré pendant qu'il est connecté | Sa connexion est fermée côté serveur (code 4403) et ses cartes assignées sont libérées |

## Stack

| Couche | Techno |
|---|---|
| Frontend | React 19 + TypeScript (Vite), dnd-kit (glisser-déposer accessible au clavier), CSS maison avec design tokens et mode sombre |
| Backend | FastAPI, WebSockets, SQLAlchemy 2, migrations Alembic |
| Base de données | SQLite par défaut (zéro installation), PostgreSQL (psycopg 3) via `DATABASE_URL` |
| Auth | bcrypt (sur un condensé SHA-256, pour les mots de passe de plus de 72 octets) + JWT (en en-tête pour REST ; pour le WebSocket, en premier message après l'ouverture, jamais dans l'URL qui finirait dans les journaux d'accès) |
| Temps réel multi-instances | Redis (pub/sub + présence partagée), optionnel via `REDIS_URL` |
| Tests | pytest, dont des tests multi-utilisateurs sur WebSocket, des courses entre requêtes simultanées, plusieurs instances reliées par Redis et la vérification des migrations (60 tests), exécutés en CI sur SQLite, PostgreSQL 16 **et** Redis 7 |

Aucun service payant.

## Lancer le projet

### Backend

```bash
cd backend
python -m venv venv
venv/Scripts/activate        # Windows (source venv/bin/activate sur macOS/Linux)
pip install -r requirements.txt
cp .env.example .env         # puis remplir JWT_SECRET
uvicorn main:app --reload --port 8040
```

Sans `JWT_SECRET`, un secret de développement est utilisé avec un avertissement au démarrage ; avec `APP_ENV=production`, le serveur refuse de démarrer.

#### Base de données et migrations

Le schéma est géré par **Alembic** (`backend/migrations/`).

- **SQLite (par défaut)** : rien à faire, les migrations sont appliquées automatiquement au démarrage du serveur (une seule instance en local, aucun risque). Une base `tandem.db` créée avant Alembic est reconnue et marquée comme étant à jour.
- **PostgreSQL** : définir `DATABASE_URL=postgresql://user:pass@hôte:5432/tandem` (converti automatiquement vers le pilote psycopg 3), puis appliquer le schéma **avant** de lancer le serveur, comme étape explicite du déploiement :

  ```bash
  alembic upgrade head
  ```

  Le serveur ne migre jamais Postgres lui-même (plusieurs instances le feraient en même temps). Taille du pool réglable avec `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (5 / 10 par défaut).

Modifier le schéma : changer `models.py`, puis générer et relire la migration (le test `test_migrations.py` échoue tant que modèles et migrations divergent) :

```bash
alembic revision --autogenerate -m "ajoute les étiquettes" --rev-id 0002
alembic upgrade head
```

### Frontend

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

Ouvre http://localhost:5176 dans **deux navigateurs différents** (ou une fenêtre normale + une privée), crée deux comptes, invite le second depuis « Partager »… et déplace des cartes.

### Tests

```bash
cd backend
pytest                                   # SQLite temporaire, aucune installation
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/tandem_test pytest   # sur Postgres (base vidée !)
TEST_REDIS_URL=redis://localhost:6379/0 REDIS_URL=redis://localhost:6379/0 pytest   # hub sur un vrai Redis
```

Les tests créent le schéma avec les migrations Alembic (pas `create_all`) et vérifient qu'il correspond exactement aux modèles. Les tests multi-instances (`test_hub_redis.py`) utilisent fakeredis en mémoire, ou un vrai Redis avec `TEST_REDIS_URL`. La CI exécute la suite sur SQLite, sur un service PostgreSQL 16, et sur un service Redis 7 (toute la suite en mode distribué).

## Architecture du temps réel

```
 navigateur A                         serveur                          navigateur B
     │  POST /api/cards/12/move  ───▶  valide + écrit en base
     │                                 revision = revision + 1  (SQL atomique)
     │  ◀─── 200 (carte à jour)        commit
     │                                 publication après la réponse ──▶  { type: "card.moved",
     │  ◀──────────── même événement (écho, ignoré car déjà appliqué)      revision: 42, … }
```

- **Écritures en REST, lecture en WebSocket** : chaque modification est validée et transactionnelle, puis diffusée. Le WebSocket ne transporte vers le serveur que l'authentification (`{ type: "auth", token }`, premier message, 5 s maximum) et des signaux éphémères (présence, « je modifie la carte 12 »).
- **Événements idempotents** côté client : appliquer deux fois le même événement, ou recevoir l'écho de sa propre action, ne change rien.
- **Mise à l'échelle** : sans `REDIS_URL`, le hub est en mémoire (un seul processus, idéal en local). Avec `REDIS_URL`, plusieurs instances se partagent le temps réel (voir ci-dessous) ; `main.py` n'en sait rien, l'interface du hub est la même.

### Plusieurs instances (Redis)

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

## Accessibilité & UX

- Glisser-déposer au clavier (Espace pour saisir, flèches, Espace pour déposer, Échap pour annuler) avec annonces pour lecteurs d'écran
- Dialogues natifs `<dialog>` (piège à focus et Échap fournis par le navigateur)
- États de chargement en squelettes, états vides, erreurs explicites, pastille d'état de connexion
- Surbrillance des cartes modifiées par les autres, avatars de présence, mode sombre automatique, mise en page mobile

## Structure

```
tandem/
├── backend/
│   ├── main.py        # Routes REST + endpoint WebSocket
│   ├── hub.py         # Rooms, présence, diffusion (en mémoire, ou entre instances via Redis)
│   ├── positions.py   # Indexation fractionnaire + rééquilibrage
│   ├── models.py      # User, Board, Membership, BoardColumn, Card, Activity
│   ├── database.py    # Moteur SQLite / Postgres, pool, application des migrations
│   ├── migrations/    # Migrations Alembic (schéma versionné)
│   ├── auth.py        # bcrypt + JWT
│   └── tests/         # positions, droits, cartes, verrou optimiste, temps réel, migrations
└── frontend/src/
    ├── useBoard.ts    # Connexion WebSocket, reconnexion, tampon, détection de trous
    ├── boardState.ts  # Reducer : événements serveur + actions optimistes (idempotents)
    └── components/    # Tableau, colonnes, cartes, modale d'édition, membres, activité
```
