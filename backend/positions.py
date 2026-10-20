"""Indexation fractionnaire pour ordonner cartes et colonnes.

Déplacer une carte ne réécrit qu'une seule ligne : sa nouvelle position est la moyenne de
celles de ses voisines. Après de très nombreuses insertions au même endroit, l'écart entre
deux voisines devient trop petit pour un float : on renumérote alors la liste entière
(« rééquilibrage »), ce qui reste rare.
"""

STEP = 1024.0
MIN_GAP = 1e-6


def position_at(sorted_positions: list[float], index: int) -> float | None:
    """Position pour insérer un élément à `index` dans une liste déjà triée.

    Retourne None si les voisins sont trop proches : il faut rééquilibrer.
    """
    index = max(0, min(index, len(sorted_positions)))
    before = sorted_positions[index - 1] if index > 0 else None
    after = sorted_positions[index] if index < len(sorted_positions) else None

    if before is None and after is None:
        return STEP
    if before is None:
        candidate = after / 2
        return candidate if after - candidate > MIN_GAP else None
    if after is None:
        return before + STEP
    if after - before <= 2 * MIN_GAP:
        return None
    return (before + after) / 2


def evenly_spaced(count: int) -> list[float]:
    return [STEP * (i + 1) for i in range(count)]
