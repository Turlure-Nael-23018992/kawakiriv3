# Dataset `omnicanal`

Une enseigne qui vend en ligne et en boutique, avec **deux systèmes clients** et
**deux catalogues**. Il sert à tester le dédoublonnage sur l'étoile : des dimensions
unifiées pour deux entités différentes (clients et produits), et deux faits réunis.

Généré par [scripts/generate_omnicanal_dataset.py](../../scripts/generate_omnicanal_dataset.py)
(seed fixe), testé par [tests/testOmnicanalDataset.py](../../tests/testOmnicanalDataset.py).

```bash
python scripts/generate_omnicanal_dataset.py
python run_v3.py examples/job_omnicanal_impose.json
```

## Tables

| table | lignes | rôle |
|---|---|---|
| `clients_web` | 200 | clients du site, `client_id` entier |
| `clients_magasin` | 160 | clients des boutiques, `code_client` texte (`M0001`) |
| `produits_web` | 60 | catalogue du site, `sku` entier |
| `articles_boutique` | 50 | catalogue des boutiques, `code_article` texte (`A-001`) |
| `ventes_web` | 1 500 | ventes en ligne |
| `ventes_boutique` | 1 200 | tickets de caisse |

Les deux espaces d'identifiants se recoupent :
- **clients** : 40 personnes sont dans les deux systèmes avec le même email, et
  20 autres seulement avec le même téléphone (pas d'email côté boutique).
  10 clients web ont un email vide (`''`), qui ne doit jamais les fusionner
  entre eux ;
- **produits** : 30 produits sont dans les deux catalogues, avec le même EAN.

Côté ventes, 40 tickets sont anonymes (pas de client) et 3 référencent un
client inexistant (`M9999`) : ce sont des orphelins.

## Résultat attendu (`job_omnicanal_impose.json`)

Les étoiles des deux canaux sont construites d'abord, chacune seule :
`ventes_web` reliée à `clients_web` et `produits_web`, `ventes_boutique` à
`clients_magasin` et `articles_boutique`. Puis l'unification imposée par le
job travaille sur ces étoiles :

| table | quoi | résultat |
|---|---|---|
| `clients_unifies` | `clients_web` + `clients_magasin`, dédoublonnés sur `email` puis `telephone` | **300** identités (40 fusions par email, 20 par téléphone) |
| `produits_unifies` | `produits_web` + `articles_boutique`, dédoublonnés sur `ean` | **80** identités |
| `ventes_web__fact_unifie`, `ventes_boutique__fact_unifie` | les faits des étoiles, clés client et produit remplacées par les ids unifiés | mêmes lignes, même ordre |
| `ventes_unifiees__fact` | les deux faits réunis, colonne d'origine `canal` | 2 700 lignes |

- `client_unifie_id` et `produit_unifie_id` sont liés à `clients_unifies` et
  `produits_unifies` ; les clés d'origine (`client_id`, `code_client`, `sku`,
  `code_article`) ne sont plus dans le fait ;
- côté boutique, 1 157 lignes sont rattachées à un client unifié ; les 40
  tickets anonymes et les 3 orphelins restent le membre inconnu
  (avertissement pour les 3 orphelins) ;
- le total des montants est celui des deux tables de ventes, au centime.
