/** Identifiants dnd-kit : les cartes et les colonnes partagent le même espace d'identifiants. */
export const cardDndId = (id: number) => `card-${id}`;
export const columnDndId = (id: number) => `column-${id}`;

export const parseCardId = (dndId: string | number) => {
  const m = /^card-(-?\d+)$/.exec(String(dndId));
  return m ? Number(m[1]) : null;
};

export const parseColumnId = (dndId: string | number) => {
  const m = /^column-(\d+)$/.exec(String(dndId));
  return m ? Number(m[1]) : null;
};
