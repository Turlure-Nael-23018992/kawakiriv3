# Dataset `commerce`

Une enseigne fictive de mobilier et de décoration, qui vend sur le web, en
magasin et par téléphone. Le dataset est synthétique et déterministe (graine
42). Il est régénéré par :

```bash
py -3.12 scripts/generate_commerce_dataset.py
```

Il est construit pour faire passer chaque cas de la chaîne v3 :
classification, liaison, extraction, grain et contrôles du hint.

## Tables

| Table | Lignes | Rôle attendu |
|---|---|---|
| `lignes_commande` | 8 914 | **fait** des ventes, une ligne par produit commandé |
| `retours` | 408 | **fait** des retours, avec un grain non unique (retours partiels) |
| `commandes` | 3 000 | en-tête : dimension de `lignes_commande`, piège si on la prend pour le fait |
| `clients` | 400 | dimension (`date_inscription` au format `jj/mm/aaaa`) |
| `produits` | 120 | dimension |
| `employes` | 40 | dimension, référencée par `commandes.vendeur_id` |
| `magasins` | 12 | dimension, `magasin_id` de 1 à 12 |

## Ce que chaque colonne de `lignes_commande` teste

| Colonne | Cas | Résultat attendu |
|---|---|---|
| `ligne_id` | identifiant entier unique | hors grain (dégénérée) |
| `ligne_uuid` | uuid unique | hors grain (dégénérée) |
| `commande_id` | FK vers une table existante | liée à `commandes` |
| `produit_id` | FK vers une table existante | liée à `produits` |
| `date_livraison` | date | jointe au calendrier partagé `lignes_commande__dim_date` |
| `code_promo` → `libelle_promo` | dépendance fonctionnelle | **une** dimension générée, et `libelle_promo` quitte le fait |
| `entrepot_code` → `entrepot_ville`, `entrepot_region` | dimension sans table propre | une dimension générée à deux attributs |
| `mode_livraison` | attribut isolé à faible cardinalité | sa propre dimension générée |
| `quantite` | entier de 1 à 10 qui recoupe `magasin_id` (1 à 12) | **mesure**, jamais liée à `magasins` (le nom ne concorde pas) |
| `prix_unitaire`, `montant_ligne`, `remise` | décimaux | mesures |
| `commentaire` | texte libre, rare (5 %), unique | dégénérée, hors grain |
| `devise` | constante (`EUR`) | écartée |
| `code_tva_legacy` | entièrement vide | écartée |

Grain attendu : `(commande_id, produit_id, date_livraison, code_promo, entrepot_code, mode_livraison)`, unique, verdict **ok**.

## Les autres pièges

- **`retours`** :
  - le grain `(ligne_id, date_retour, motif_code)` n'est **pas** unique :
    8 lignes sont en doublon, parce que certains retours partiels se
    répètent le même jour pour le même motif ;
  - le rapport propose `retour_id` pour compléter le grain ;
  - `motif_code` → `motif_libelle` est extrait ensemble ;
  - verdict **needs_review**. Un second avertissement est attendu :
    `lignes_commande`, à laquelle `retours` se lie, a plus de lignes que
    `retours`. Pour un fait de retours, c'est normal ; c'est une limite
    connue de ce contrôle.
- **`commandes` prise comme fait** (voir `job_commerce_impose.json`), trois
  avertissements, et donc **needs_review** :
  - la table n'a aucune mesure ;
  - `lignes_commande.commande_id` la référence avec plus de lignes (motif
    en-tête / lignes) ;
  - le grain contient des clés nulles : `magasin_id` et `vendeur_id` sont
    vides pour les commandes web, ce sont des FK nullables.
- **`commandes.vendeur_id` → `employes.employe_id`** : un lien sans
  concordance de nom, accepté parce que la colonne est nommée comme une clé.

## Jobs

| Job | Hint | Résultat observé (run réel, 2026-10-28) |
|---|---|---|
| `job_commerce.json` | LLM, besoin sur les ventes **et** les retours | `lignes_commande` (ok) + `retours` (needs_review) |
| `job_commerce_ventes_seules.json` | LLM, besoin sur les ventes seulement | `lignes_commande` (ok) |
| `job_commerce_impose.json` | imposé, sans LLM | les trois faits ci-dessus |

Le choix du LLM n'est pas déterministe : ces résultats sont ceux d'un run,
pas une garantie.
