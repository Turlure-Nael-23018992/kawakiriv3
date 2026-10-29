# Dataset `abonnements`

Un éditeur SaaS B2B fictif (facturation par abonnement), synthétique et
déterministe (graine 2026). Il est régénéré par :

```bash
py -3.12 scripts/generate_abonnements_dataset.py
```

Contrairement à [commerce](../commerce/README.md) ou [marge](../marge/README.md),
qui vérifient que la chaîne v3 **réussit** les cas qu'elle sait traiter, ce
dataset vise ses **limites documentées** (section "Pas encore fait" du
[README principal](../../README.md)) : historique SCD, clé composite, table
factless, auto-référence, dépendance sans sens métier. Rien ici n'est censé
sortir "propre" — le but est de vérifier que la chaîne échoue de façon
visible (avertissement, `needs_review`) plutôt que silencieusement.

## Tables

| Table | Lignes | Rôle attendu | Le piège |
|---|---|---|---|
| `abonnements` | 253 | **fait** : une ligne par version d'abonnement | historique SCD non filtré |
| `factures` | 2 963 | **fait** : une ligne par facture ou avoir | auto-référence, fausse clé |
| `evenements_usage` | 7 000 | **fait** : un événement d'usage d'une fonctionnalité | FK orpheline, recoupement de plage, colonne dérivée |
| `plans_fonctionnalites` | 60 | **table pont** (many-to-many), aucune mesure | clé composite, factless |
| `clients` | 220 | dimension | date dans deux formats |
| `plans` | 6 | dimension | deux candidats PK ex æquo |
| `fonctionnalites` | 20 | dimension | incomplète par construction (21-23 manquants) |

## Les pièges

### 1. Historique SCD non filtré (`abonnements`) -- *limite partiellement comblée*

33 clients sur 220 ont changé de plan en cours de vie : leur abonnement
apparaît en **deux lignes** (`abonnement_logique_id` identique,
`abonnement_id` différent), comme un vrai snapshot dbt non dédupliqué
(`DBT_VALID_FROM`/`DBT_VALID_TO`, `SNAPSHOT_DATE`).

Sommer `tarif_mensuel_effectif` sur toutes les lignes `statut = 'actif'`
donne un MRR de **15 687 €**, contre un MRR réel de **15 391 €** (une seule
version par `abonnement_logique_id`) -- une surestimation de **296 €**
(+1,9 %). `factHintCheck.py` détecte maintenant la paire `DBT_VALID_FROM`/
`DBT_VALID_TO` encore variable et émet un avertissement (verdict
**needs_review**, plus de faux **ok**) -- mais la chaîne ne filtre toujours
pas la version courante toute seule : c'est au job de la déclarer via
`rowFilters`. `job_abonnements_scd_filtered.json` le fait
(`"abonnements": "DBT_VALID_TO IS NULL"`) : le fait tombe à 138 lignes et
l'avertissement se taît, `DBT_VALID_TO` étant devenu constant (nul partout).

### 2. Table pont factless (`plans_fonctionnalites`) -- *limite documentée*

60 lignes, deux clés étrangères, **aucune mesure**, grain `(plan_id,
fonctionnalite_id)` déjà composite et déjà unique. Imposée comme fait
(`job_abonnements_bridge_impose.json`), elle doit produire exactement les
deux avertissements vus sur `examples/reel/` avant nettoyage :
*"aucune mesure : table de faits sans fait"* -- mais ici c'est **correct**,
pas un bug de délimiteur : une table pont n'est pas un fait, et la chaîne
n'a pas de notion de "junk dimension" ou de table de liaison pour le dire
autrement que par un rejet.

### 3. Auto-référence (`factures.facture_origine_id -> factures.facture_id`) -- *limite comblée*

160 avoirs (`type_doc = 'avoir'`), chacun avec `facture_origine_id` rempli
et pointant vers une facture existante de type `facture` -- un lien **dans
la même table**. Aucun autre dataset de ce dépôt n'a de cas d'auto-référence
sur un fait (seul `fact_orders.order_id` dans le stress test v2 en avait un,
synthétique). `facture_origine_id` est nullable (vide sauf pour les avoirs) :
couverture partielle normale, à ne pas confondre avec une FK cassée.

`linkGraph.py` et `factLinker.py` acceptent maintenant ce lien : une table
peut se lier à elle-même, mais seulement sur une colonne nullable --
`facture_id` (jamais nul) ne serait, lui, jamais candidat à
l'auto-référence, sans quoi toute clé obligatoire se recouvrirait
trivialement avec elle-même.

### 4. Fausse clé qui se réinitialise (`factures.numero_piece`)

2 963 lignes, seulement **15 valeurs distinctes** : `numero_piece`
recommence à 1 chaque année pour chaque client (comme un vrai numéro de
pièce comptable). Pris isolément ce n'est jamais une clé -- mais combiné à
`client_id` et l'année de `date_facture`, c'est la vraie clé métier de la
facture. La chaîne v3 ne teste que des colonnes seules (voir "Clés
composites" dans les limites documentées) : `numero_piece` doit être
écarté par les statistiques (trop de doublons), jamais reconstruit.

### 5. FK orpheline vers une dimension incomplète (`evenements_usage.fonctionnalite_id`)

207 événements (56+85+66, ~3 %) référencent les fonctionnalités **21, 22 et
23**, qui n'existent dans aucune ligne de `fonctionnalites.csv` (dimension
volontairement limitée à 1-20 -- une fonctionnalité retirée du catalogue
mais encore présente dans de vieux événements). Le lien
`evenements_usage.fonctionnalite_id -> fonctionnalites.fonctionnalite_id`
doit être accepté (le nom concorde, la couverture est de 97 %), et ces 3 %
doivent ressortir comme des clés non résolues -- pas comme un rejet du lien.

### 6. Recoupement de plage (`evenements_usage.nb_appels` vs `fonctionnalite_id`)

`nb_appels` (mesure, 1 à 20) recoupe exactement la plage de
`fonctionnalite_id` (1 à 20) -- même piège que `quantite`/`magasin_id` dans
[commerce](../commerce/README.md) et `quantite`/`entrepot_id` dans
[erp](../erp/README.md). Ne doit jamais être lié : le nom ne concorde pas.

### 7. Colonne dérivée sans valeur métier (`evenements_usage.semaine_iso`) -- *limite comblée*

`semaine_iso` est une **fonction déterministe** de `date_evenement` (le
numéro de semaine ISO) -- une dépendance fonctionnelle réelle (100 % des
lignes), mais sans intérêt métier propre.

`dimensionExtractor.py` la reconnaît maintenant comme dérivée d'une date du
fait et la fond dans le calendrier partagé (`evenements_usage__dim_date`)
au lieu de la dupliquer dans sa propre dimension -- y compris quand le hint
LLM la classe à tort en mesure plutôt qu'en clé (jamais quand c'est
l'appelant qui l'a désignée mesure : son choix n'est jamais remis en
question).

### 8. Deux candidats PK ex æquo (`plans`)

Table de 6 lignes où `plan_id` **et** `nom_plan` sont chacun uniques --
candidats à égalité, comme dans le stress test v2 (`dim_categories`,
`dim_products`, ...), mais jamais exploité dans un dataset v3 existant.

### 9. Date dans deux formats (`clients.date_inscription`)

61 lignes en `jj/mm/aaaa`, 159 en ISO, dans la **même colonne** -- pas un
format unique différent de l'ISO (comme `commerce.date_inscription`), mais
un vrai mélange. Le format dominant (ISO, 72 %) doit être lu ; les lignes
`jj/mm/aaaa` sont censées échouer le parsing (valeur nulle ou mal typée).

## Jobs

| Job | Hint | Ce qu'il fait |
|---|---|---|
| `job_abonnements.json` | LLM, besoin sur le MRR, la facturation et l'usage | le LLM doit choisir parmi `abonnements`, `factures`, `evenements_usage` **et** `plans_fonctionnalites` -- voir si la table pont est écartée |
| `job_abonnements_bridge_impose.json` | imposée, sans LLM | `plans_fonctionnalites` imposée comme fait : verdict attendu **rejected**, "aucune mesure" |
| `job_abonnements_scd_filtered.json` | imposée, sans LLM | `abonnements` avec `rowFilters: {"abonnements": "DBT_VALID_TO IS NULL"}` -- vérifie que le filtre élimine le double-comptage SCD (253 -> 138 lignes) et fait taire l'avertissement historique une fois `DBT_VALID_TO` neutralisé |

Le choix du LLM n'est pas déterministe. Les chiffres ci-dessus (33 clients,
296 €, 160 avoirs, 207 événements orphelins, 61/159 dates) viennent du
dataset généré avec la graine 2026 et ne changent pas d'un run à l'autre --
seule l'interprétation que la chaîne v3 en fait peut varier.
