# kawakiriv3

Reconstruction d'un schéma en étoile **en partant de la table de faits**.

En v2, le rôle de chaque table (fait ou dimension) se déduisait de la
topologie d'un graphe de relations. En v3, la table de faits est **donnée** :
soit par l'appelant, soit par un hint LLM lu à partir du besoin métier. Tout
le reste se construit ensuite de façon déterministe, à partir des colonnes de
cette table, vers l'extérieur.

Le déroulé complet d'un run, étape par étape, est décrit dans
[PIPELINE.md](PIPELINE.md).

## Principe

Une fois les identifiants de ligne mis de côté, la clé d'un fait est la
combinaison de ses clés de dimension. Pour chaque colonne de la table de
faits :

1. **Classification** ([columnClassifier.py](kawakiriv3/FactAnalysis/columnClassifier.py)) :
   - une colonne unique sur chaque ligne (`vente_id`, un uuid) est un
     **identifiant de ligne**, hors grain ;
   - une colonne décimale est une **mesure** ;
   - une date est une clé de dimension temporelle ;
   - une valeur discrète qui se répète est une **clé candidate** ;
   - un texte quasi unique est un libellé, qui reste dans le fait hors grain ;
   - une colonne vide ou constante est écartée ;
   - une colonne **technique** est écartée : métadonnées de chargement dbt,
     Airbyte, Fivetran, `LOADED_AT`, `FILE_NAME`…

   Les statistiques seules ne suffisent pas toujours. Un identifiant entier
   comme `RPAN8` chez JD Edwards ne suit aucune convention de nom. Des
   **rôles imposés** ([columnRoles.py](kawakiriv3/FactAnalysis/columnRoles.py))
   tranchent alors : clé, mesure ou colonne exclue. Pour chaque colonne,
   l'ordre de priorité est le suivant :
   - **l'appelant** (`columnRoles` du job) ;
   - **le LLM** (le hint des colonnes, voir plus bas) ;
   - **le motif de nom technique** ;
   - **les statistiques**.

   L'unicité reste toujours un fait statistique : une colonne unique sur
   chaque ligne reste un identifiant de ligne, même si quelqu'un l'appelle
   clé. Le rapport indique, pour chaque colonne, qui a décidé de son rôle.
2. **Liaison** ([factLinker.py](kawakiriv3/FactAnalysis/factLinker.py)) :
   - chaque clé candidate est testée contre les clés des autres tables
     (au moins 95 % de recouvrement, via le `SourceJoinEngine` repris de la
     v2). On ne teste que les colonnes du fait, jamais toutes les paires de
     tables ;
   - un entier qui n'est pas nommé comme une clé (`quantite`) n'est lié que
     si les noms corroborent le lien. Sinon, c'est une mesure : ses petites
     valeurs recoupent par hasard n'importe quelle plage d'identifiants.
3. **Extraction** ([dimensionExtractor.py](kawakiriv3/FactAnalysis/dimensionExtractor.py)) :
   - une clé qui ne se lie à rien sort du fait et devient une **dimension
     générée** ;
   - les colonnes sont d'abord regroupées par dépendance fonctionnelle :
     `code_produit -> libelle_produit, categorie` donne une seule dimension
     `ventes__dim_code_produit`, et les deux attributs quittent le fait ;
   - une clé désignée par l'appelant ou par le LLM n'est jamais absorbée
     comme attribut d'une autre clé : une dépendance trouvée sur les données
     peut être fortuite ;
   - **toutes les dates partagent un seul calendrier**, `<fait>__dim_date`,
     avec `annee`, `trimestre`, `mois`, `jour` et `jour_semaine` : c'est la
     date « role-playing » de Kimball. Une date stockée en texte est lue
     avec son format dominant (`jj/mm/aaaa`…), et un horodatage se joint au
     calendrier par sa partie date ;
   - le fait réécrit s'appelle `<fait>__fact`. La table source n'est jamais
     modifiée.
4. **Vérification du grain** ([grainVerifier.py](kawakiriv3/FactAnalysis/grainVerifier.py)) :
   - « grain = toutes les clés » est une hypothèse, mesurée et non supposée :
     on compare `COUNT(*)` au nombre de tuples distincts ;
   - une clé nulle est le membre « inconnu » de sa dimension. Elle compte
     comme une valeur, donc deux lignes inconnues identiques font bien un
     doublon. Les lignes concernées sont comptées à part
     (`unknownKeyRows`), à titre d'information ;
   - si le grain n'est pas unique, on cherche la dimension dégénérée (un
     numéro de ticket, une référence) qui le rend unique.
5. **Contrôles du hint** ([factHintCheck.py](kawakiriv3/FactAnalysis/factHintCheck.py)) :
   - un fait sans mesure ;
   - une table plus volumineuse qui référence le fait (motif en-tête /
     lignes : le grain demandé est sans doute plus fin) ;
   - une dimension liée qui a plus de lignes que le fait.

   Ces contrôles ne rejettent jamais le hint : ils envoient le modèle en revue.

**Verdict** :
- `ok` : grain unique, aucun avertissement ;
- `needs_review` : au moins un avertissement, un lien ambigu, ou un grain
  non unique ;
- `rejected` : aucune clé de dimension.

## Faits assemblés

Quand aucune table ne porte seule les mesures demandées, parce qu'elles sont
réparties sur plusieurs tables, le fait est **assemblé** par le code
([FactAssembly/](kawakiriv3/FactAssembly/)) avant de suivre la chaîne
habituelle.

1. **Graphe des liens physiques** ([linkGraph.py](kawakiriv3/FactAssembly/linkGraph.py)) :
   - chaque colonne de chaque table est testée contre les clés des autres
     tables (le `SourceJoinEngine`) ;
   - chaque lien a un sens et une cardinalité : `N:1`, ou `1:1` quand la
     colonne source est elle-même unique ;
   - chaque lien est décidé en trois étages, voir [Arbitrage des liens](#arbitrage-des-liens) ;
   - le graphe est montré au LLM, sert à l'assemblage, et guide la liaison
     des colonnes du fait (étape 2 du principe).
2. **Le LLM**, ou le job (`assembledFacts`), décrit le fait :
   - l'**ancre**, la table dont une ligne est le grain ;
   - les **mesures**, avec leur table et leur additivité (`additive`,
     `semi_additive`, `non_additive`) ;
   - les **clés** à descendre d'une table plus grossière (le client, la
     date ou le canal d'un en-tête de commande, pour ses lignes).
3. **L'algèbre du grain** ([factAssembler.py](kawakiriv3/FactAssembly/factAssembler.py))
   décide, pour chaque table, ce qui est permis :

   | relation à l'ancre | mesure | clé |
   |---|---|---|
   | même grain (liens 1:1) | copiée | copiée |
   | plus grossière (lien N:1 depuis l'ancre) | additive : répartie (`egal`, `prorata:<mesure>`), sinon refusée ; non additive : répétée ; semi-additive : refusée | copiée |
   | plus fine (lien N:1 vers l'ancre) | agrégée avant la jointure (`sum` par défaut si additive) | refusée |
   | aucun chemin | refusée | refusée |

4. **Contrôles**, qui envoient le modèle en revue :
   - le fait assemblé a exactement le nombre de lignes de l'ancre ;
   - **réconciliation** : chaque mesure additive a le même total dans le
     fait que dans sa table source. Un écart signale des lignes orphelines,
     ou comptées deux fois ;
   - chaque élément refusé, et chaque table atteinte par plusieurs chemins
     aussi courts.

Le fait assemblé passe ensuite par la classification, la liaison,
l'extraction et le grain, comme une table existante. Les tables fondues
dans le fait (l'ancre, ses tables au même grain ou plus fines) ne sont plus
des cibles de liens. Voir le dataset [marge](examples/marge/README.md) et
sa comparaison avant/après.

## Arbitrage des liens

Un recouvrement de valeurs ne prouve pas un lien : deux clés séquentielles
se recouvrent par hasard. À l'inverse, des noms discordants ne prouvent pas
l'absence de lien : chez JD Edwards, `RPAN8` et `ABAN8` sont le même numéro
d'adresse. Chaque lien candidat est donc décidé en trois étages
([linkGraph.py](kawakiriv3/FactAssembly/linkGraph.py)).

1. **Déterministe**, sans appel, pour la grande majorité :
   - **accepté** quand les noms concordent sur un concept qui a du sens
     (`client_id -> clients.client_id`, ou `client_id -> clients.id`). Un
     nom générique partagé (`id`, `ref`, `code`…) ne prouve rien ;
   - **accepté** quand un lien 1:1 est couvert dans les deux sens (une clé
     partagée), sauf si c'est une plage d'entiers dense : 1..N des deux
     côtés peut être deux séquences de même longueur ;
   - **refusé** quand un lien 1:1 n'est couvert que dans un sens (moins de
     50 % dans l'autre) : `clients.client_id` (1 à 300) ne couvre que 4 %
     des `ligne_id` ;
   - **refusé** quand une autre cible de la même colonne a un nom concordant.
2. **Douteux**, arbitré par le LLM : une forte couverture avec des noms
   discordants, des noms concordants avec une couverture de 80 à 95 %, ou un
   1:1 partiellement partagé.
   - **Un seul appel** pour tous les liens douteux du run (au plus 60) :
     `POST /arbitrate-links`, outil forcé, identifiants de liens restreints
     par un `Literal`, un verdict exactement par lien, nouvelle tentative
     non aveugle. Le LLM ne voit que la structure et les statistiques.
   - **Cache** ([linkVerdictCache.py](kawakiriv3/FactAssembly/linkVerdictCache.py)) :
     chaque verdict est gardé par signature du lien (tables, colonnes,
     cardinalité, couvertures arrondies au pourcent), dans `linkCachePath`.
     Le même dataset donne le même graphe d'un run à l'autre, sans appel.
     Un cache est réutilisé même dans un run sans LLM.
   - Un lien que personne n'arbitre (pas d'appel LLM dans le run, ou échec
     de l'appel) est **refusé par prudence**.
3. **L'appelant** a le dernier mot : `declaredLinks` accepte un lien, même
   sous le seuil de recouvrement, et `forbiddenLinks` en interdit un.

Le rapport liste chaque lien candidat, avec son statut, qui l'a décidé et
pourquoi. Les verdicts s'appliquent aussi à la liaison des colonnes du
fait : un lien accepté par le LLM ou le job est lié même sans nom de clé,
un lien qu'ils ont refusé ne l'est jamais. Voir le dataset
[jde_liens](examples/jde_liens/README.md).

## Le hint LLM

Le hint est fourni par `POST /propose-fact-tables` du service
`kawakiri-langgraph` (nœud `propose_fact_tables`, graphe
`build_fact_hint_graph`) :
- **ce que le LLM reçoit** : le besoin métier, plus la structure et les
  statistiques des tables (types, cardinalités, unicité, descriptions) et
  les liens physiques détectés. Jamais une valeur réelle ;
- **ce qu'il renvoie** : une ou plusieurs tables existantes, ou un ou
  plusieurs faits à assembler, le plus pertinent en premier. Il n'en
  propose plusieurs que si le besoin porte sur plusieurs processus
  distincts ;
- **les garde-fous** : le nom de table est restreint par un `Literal` aux
  tables du payload, et une erreur de validation est renvoyée au modèle
  avant une nouvelle tentative. Côté v3,
  [factHintAdvisor.py](kawakiriv3/FactHint/factHintAdvisor.py) revérifie
  chaque nom.

Ensuite, **un agent par table de faits**, en parallèle (nœud
`classify_fact_columns`), liste les colonnes que les statistiques ne savent
pas trancher : `keyColumns`, `measureColumns` et `technicalColumns`. Chaque
colonne figure dans une seule liste au plus, et son nom est restreint à
ceux de la table. Côté v3, un nom inconnu est écarté et signalé.

Un fait assemblé n'a pas besoin de cette étape : ses mesures et ses clés
viennent avec lui.

Quand le job impose `factTables`, le LLM ne choisit plus les tables, mais il
classe toujours leurs colonnes. Avec `"columnHint": false`, le run ne fait
plus aucun appel LLM. Un job qui impose `assembledFacts` seuls ne fait aucun
appel non plus.

## Unification (post-traitement)

Unifier des clients présents sur deux canaux, ou réunir les ventes de
plusieurs sources en un seul fait, se fait en deux temps :

1. **l'étoile** : la pipeline construit une étoile par fait, chaque source
   seule (le fait `<fait>__fact`, ses tables reliées, ses dimensions
   extraites, son calendrier) ;
2. **le dédoublonnage sur l'étoile** ([StarUnification/](kawakiriv3/StarUnification/)),
   seulement si quelque chose le demande : le job (`unification`, appliquée
   telle quelle), sinon le hint (`unificationRequested`), et alors
   `POST /propose-unifications` reçoit les étoiles (structure seulement) et
   décrit l'unification, y compris ce qui en découle (des clients unifiés
   appellent en général des ventes réunies).

Le temps 2 ne lit que les tables de l'étoile et les rôles qu'elle a
décidés : aucune nouvelle classification, aucune recherche de liens, aucun
rôle recalculé. Il ne peut donc pas casser une étoile validée. Il fait :

- **des dimensions unifiées** : l'union des dimensions des étoiles qui
  décrivent la même entité (`loyalty_cards` et `web_customers`), puis un
  dédoublonnage sur des identifiants forts (email, téléphone). La table
  unifiée porte tous les attributs, la présence par source
  (`present_web`) et la clé de chaque source (`<clé>_web`) ; chaque
  attribut vient de la fiche prioritaire (date de modification, sinon la
  source jugée la plus fiable) ;
- **le remplacement des clés** : dans chaque fait, la clé vers une dimension
  fondue est remplacée par l'id unifié (`<fait>__fact_unifie`, mêmes lignes,
  même ordre), via la table de correspondance ;
- **des faits réunis**, si demandé : l'union des faits, avec la colonne
  d'origine (le canal). Les rôles, les mesures et les liens sont repris des
  étoiles ; un produit aligné sans être unifié devient une dimension
  conforme, les dates un calendrier commun. Le fait réuni remplace les faits
  d'origine dans le modèle final.

Le temps 2 reprend l'étoile telle quelle, défauts compris : on corrige
l'étoile, pas l'unification. Le détail est dans [PIPELINE.md](PIPELINE.md)
§7bis.
Les problèmes constatés et les correctifs à faire sont dans
[PROBLEMES.md](PROBLEMES.md).

## Sources

Le module [Sources/](kawakiriv3/Sources/) définit une interface commune,
`SourceConnector`, qui expose `listTables`, `listColumns`, `fetchDataFrame`
et `loadInto`. Tout le pipeline tourne ensuite sur DuckDB.

| `kind` | Connecteur | Paramètres |
|---|---|---|
| `csv` | `CsvSource` : `read_csv` natif de DuckDB, une table par fichier | `path` (dossier ou fichier), `delimiter`, `header`, `encoding` |
| `clickhouse` | `ClickHouseSource` (`clickhouse-connect`) | `database`, `host`, `port`, `user`, `password`, `secure`, `rowLimit` |
| `starrocks` | `StarRocksSource` (protocole MySQL, `PyMySQL`) | `database`, `host`, `port` (9030, le port de requête du FE), `user`, `password`, `catalog` (catalogue externe : Hive, Iceberg, JDBC…), `rowLimit` |
| `postgres` | `PostgresSource` (`psycopg` 3, session en lecture seule) | `database`, `schemas` (liste, `["public"]` par défaut ; avec plusieurs schémas les tables se nomment `schema.table`, chargées dans DuckDB sous leur nom nu), `host`, `port` (5432), `user`, `password`, `sslmode`, `rowLimit` |

Ce qui ne doit pas être versionné avec le job (hôte, identifiants d'une
source distante) s'écrit `${VAR}` dans le bloc `source` : la valeur est lue
dans l'environnement, sinon dans `kawakiriv3/.env` (voir `.env.example`). Une
variable définie nulle part arrête le run avant toute connexion.

```json
"source": {"kind": "starrocks", "host": "${STARROCKS_HOST}", "port": "${STARROCKS_PORT}",
           "user": "${STARROCKS_USER}", "password": "${STARROCKS_PASSWORD}", "database": "ventes"}
```

Pour ajouter une source : une sous-classe de `SourceConnector`,
enregistrée dans `_SOURCE_KINDS` ([Sources/__init__.py](kawakiriv3/Sources/__init__.py)).

## Lancer

```bash
pip install -r requirements.txt          # Python >= 3.10
# service du hint (sauf si le job impose "factTables")
cd ../kawakiri-langgraph && uvicorn langgraph_advisor.service:app --port 8001
cd ../kawakiriv3 && python run_v3.py examples/job_csv_ventes.json
pytest
```

La sortie va dans `output/` : `v3_results.json`, plus `v3_report.md` (un
diagramme Mermaid par étoile et le rôle de chaque colonne, avec sa raison).
La forme d'un job est documentée dans [jobPayload.py](kawakiriv3/jobPayload.py).

## Repris de kawakiriv2

Ces modules sont copiés, pas importés : les deux projets évoluent
indépendamment.
- `Profiling`, `Identifiability`, `PrimaryKey` et `DuckDbManagement`, à
  l'identique ;
- `FunctionalGroup`, sans le veto LLM ;
- `Schema`, sans les tables logiques ;
- `SourceJoin`, avec en plus `sourceColumnsByTable` et `targetTables` pour
  restreindre les colonnes sources et les tables cibles.

## Pas encore fait

- **Flocons** : on ne suit pas les liens sortants des dimensions liées.
- **Dimensions conformes** : chaque fait a ses propres dimensions générées
  et son propre calendrier, même quand deux faits partagent une clé.
- **Clés composites** : les liens physiques ne se testent que sur une
  colonne. Un document JDE (`DOC + DCT + KCO + SFX`) ne se relie pas encore.
- **Fait existant imposé** : imposer une table avec `factTables` ne
  déclenche jamais d'assemblage, même si ses mesures sont ailleurs.
- **Clés déclarées par la source** : pas encore de PK/FK lues depuis le
  catalogue de la source. Le job peut toutefois imposer des rôles de
  colonnes (`columnRoles`).
- **Historique SCD** : un snapshot dbt contient plusieurs versions d'une
  même ligne (9 documents en deux versions dans l'extrait JDE). Si le grain
  les distingue, leurs montants comptent deux fois. `factHintCheck.py`
  détecte la paire technique `*_valid_from`/`*_valid_to` encore variable et
  avertit (`needs_review`), et le job peut filtrer la version courante à la
  source via `rowFilters` -- mais la chaîne ne le fait jamais elle-même,
  c'est au caller de le déclarer. Voir le dataset
  [abonnements](examples/abonnements/README.md).
- **Junk dimension** : chaque petit attribut isolé (un `canal`, un booléen)
  devient sa propre dimension, au lieu d'être regroupé.
- **Dépendances fonctionnelles fortuites** : une dépendance peut être vraie
  dans les données sans avoir de sens métier (par exemple, chaque client
  n'achète que sur un canal). Elle est appliquée telle quelle, sans revue LLM.
