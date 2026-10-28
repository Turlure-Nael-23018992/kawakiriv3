# Kawakiri v3 -- rapport

**Besoin metier** : Analyser la marge brute de chaque ligne de commande.

**Choix des tables de faits** : imposee par l'appelant

<details><summary>Liens physiques detectes (13)</summary>

- commandes.client_id -> clients.client_id (N:1, couverture 100%)
- couts_achat.ligne_id -> lignes_commande.ligne_id (1:1, couverture 100%)
- couts_achat.ligne_id -> tarification_lignes.ligne_id (1:1, couverture 100%)
- expeditions.ligne_id -> couts_achat.ligne_id (N:1, couverture 97%)
- expeditions.ligne_id -> lignes_commande.ligne_id (N:1, couverture 100%)
- expeditions.ligne_id -> tarification_lignes.ligne_id (N:1, couverture 100%)
- lignes_commande.commande_id -> commandes.commande_id (N:1, couverture 100%)
- lignes_commande.ligne_id -> couts_achat.ligne_id (1:1, couverture 97%)
- lignes_commande.ligne_id -> tarification_lignes.ligne_id (1:1, couverture 100%)
- lignes_commande.produit_code -> produits.produit_code (N:1, couverture 100%)
- stocks_journaliers.produit_code -> produits.produit_code (N:1, couverture 100%)
- tarification_lignes.ligne_id -> couts_achat.ligne_id (1:1, couverture 97%)
- tarification_lignes.ligne_id -> lignes_commande.ligne_id (1:1, couverture 100%)

</details>

## lignes_commande -- **needs_review**

Pourquoi cette table : imposee par l'appelant

```mermaid
erDiagram
    "lignes_commande__fact" {
        numeric ligne_id FK
        numeric commande_id FK
        string produit_code FK
        numeric quantite
    }
    "tarification_lignes" ||--o{ "lignes_commande__fact" : "ligne_id"
    "commandes" ||--o{ "lignes_commande__fact" : "commande_id"
    "produits" ||--o{ "lignes_commande__fact" : "produit_code"
```

Grain : (ligne_id, commande_id, produit_code) -- unique sur 7403 lignes

Avertissements :
- expeditions.ligne_id (10949 lignes) reference lignes_commande.ligne_id (7403 lignes) : le grain demande est peut-etre celui de expeditions

| colonne | role | decide par | raison |
|---|---|---|---|
| ligne_id | cle etrangere (liee) | statistiques | 100% des valeurs retrouvees dans tarification_lignes.ligne_id (noms concordants) |
| commande_id | cle etrangere (liee) | statistiques | 100% des valeurs retrouvees dans commandes.commande_id (noms concordants) |
| produit_code | cle etrangere (liee) | statistiques | 100% des valeurs retrouvees dans produits.produit_code (noms concordants) |
| quantite | mesure | statistiques | entier sans lien corrobore par le nom vers une autre table : mesure |

