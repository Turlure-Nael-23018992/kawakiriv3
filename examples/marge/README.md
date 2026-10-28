# Dataset `marge`

Un grossiste fictif de fournitures de bureau (B2B), qui vend sur le web, par
commercial et par téléphone. Le dataset est synthétique et déterministe
(graine 7). Il est régénéré par :

```bash
py -3.12 scripts/generate_marge_dataset.py
```

Il est construit pour le **fait assemblé** : le besoin, la marge par ligne
de commande, ne trouve ses mesures dans **aucune table seule**. Elles sont
réparties sur cinq tables, à trois grains différents.

## Tables

| Table | Lignes | Grain | Ce qu'elle teste |
|---|---|---|---|
| `lignes_commande` | 7 403 | une ligne de commande | l'**ancre** : le grain demandé, mais une seule mesure (`quantite`) |
| `tarification_lignes` | 7 403 | une ligne de commande | **même grain** (1:1 par `ligne_id`) : `montant_ht`, `montant_tva` (additives), `prix_unitaire`, `taux_remise` (non additives) |
| `couts_achat` | 7 195 | une ligne de commande | **même grain**, mais 3 % des lignes n'ont pas de coût : 208 valeurs nulles, jamais inventées |
| `expeditions` | 10 949 | un colis | **plus fine** : 1 à 3 colis par ligne, 8 % des lignes jamais expédiées. `cout_transport` et `poids_kg` sont à sommer par ligne |
| `commandes` | 2 400 | une commande | **plus grossière** : `frais_port` est facturé une fois par commande et doit être **réparti** sur ses lignes. Porte aussi `client_id`, `date_commande` et `canal`, les clés à descendre sur la ligne |
| `stocks_journaliers` | 9 000 | produit × jour | **hors périmètre** : un autre processus (un stock est semi-additif), relié aux produits mais à aucune ligne |
| `clients`, `produits` | 300, 150 | | dimensions |

## Les pièges

- **6 colis orphelins** dans `expeditions` : leur `ligne_id` n'existe pas
  (une migration ancienne). Ils ne peuvent être rattachés à aucune ligne.
  La réconciliation doit le dire : un écart de 38,05 € sur `cout_transport`,
  de 15,40 kg sur `poids_kg` et de 6 sur le nombre de colis. Le verdict est
  donc **needs_review**, et c'est voulu.
- **`frais_port` recopié** sur chaque ligne de sa commande serait compté
  autant de fois que la commande a de lignes. Sans règle de répartition, il
  doit être refusé ; au prorata de `montant_ht`, il doit se réconcilier au
  centime près (3 233,80 €).
- **`client_id` (1 à 300) tombe dans la plage de `ligne_id` (1 à 7 403)** :
  le lien `clients.client_id -> lignes_commande.ligne_id` est couvert à
  100 %, mais il est fortuit. Le graphe des liens doit l'écarter.
- **`quantite` et `stock_fin_jour`** sont des entiers qui ne sont pas des
  clés.

## Jobs

| Job | Ce qu'il fait |
|---|---|
| `job_marge.json` | hint LLM, besoin complet : le LLM choisit lui-même entre une table existante et un fait assemblé |
| `job_marge_impose.json` | fait assemblé imposé (`assembledFacts`), sans LLM : la référence déterministe |
| `job_marge_lignes_impose.json` | `lignes_commande` imposée comme fait existant, sans LLM : ce que donne la v3 sans assemblage |

## Avant et après l'assemblage (runs réels, 2026-10-28)

Les sorties complètes sont dans [resultats/avant/](resultats/avant/) et
[resultats/apres/](resultats/apres/).

| | Avant | Après |
|---|---|---|
| **Hint LLM** (`job_marge.json`) | `lignes_commande` choisie comme fait existant : **1 mesure** (`quantite`), verdict **ok** (faux positif : tout le reste est perdu). Le LLM avait pourtant vu, dans son raisonnement, que les montants étaient dans des tables satellites | **fait assemblé** proposé par le LLM, ancre `lignes_commande` : **7 mesures**, 3 clés descendues de `commandes`, frais de port répartis au prorata de `montant_ht`, stocks écartés. Verdict **needs_review**, à cause des seuls colis orphelins |
| **Fait assemblé imposé** (`job_marge_impose.json`) | n'existait pas | **10 mesures** (dont `nb_colis` et les deux non additives), grain unique, 4 montants réconciliés au centime, 3 écarts dus aux orphelins |
| **Fait existant imposé** (`job_marge_lignes_impose.json`) | 1 mesure, `tarification_lignes` prise pour une dimension (ses montants y restent enfermés) | inchangé : imposer une table existante ne déclenche pas d'assemblage |

L'étoile du fait assemblé (job imposé) :
- clés étrangères : `commande_id -> commandes`, `produit_code -> produits`,
  `client_id -> clients` (ce dernier descendu de `commandes`) ;
- dimensions générées : le calendrier `marge_lignes__dim_date` (date de
  commande) et `marge_lignes__dim_canal` ;
- grain `(commande_id, produit_code, client_id, date_commande, canal)`,
  unique sur 7 403 lignes ; `ligne_id` reste une dimension dégénérée.

Le choix du LLM n'est pas déterministe : ces résultats sont ceux d'un run,
pas une garantie.

## Avec l'arbitrage des liens (2026-10-28)

Sorties dans [resultats/apres_arbitrage/](resultats/apres_arbitrage/).
Sur 24 liens candidats, 18 sont décidés sans appel (13 acceptés par les
noms, 3 refusés par les noms, 3 coïncidences 1:1 refusées par les
statistiques). Les 5 liens douteux (`quantite` et `stock_fin_jour` vers des
clés séquentielles) sont tous refusés par le LLM, en un appel, puis lus
depuis le cache au run suivant. Le fait assemblé est inchangé : 7 mesures,
grain unique, mêmes écarts dus aux colis orphelins.
