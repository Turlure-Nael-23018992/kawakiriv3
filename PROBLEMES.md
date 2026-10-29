# Problèmes connus et correctifs à faire

Constatés sur les runs réels Postgres (`examples/job_postgres.json`) et sur le
jeu `omnicanal`, après la refonte « l'étoile d'abord, puis le dédoublonnage
sur l'étoile » ([PIPELINE.md](PIPELINE.md) §7bis). Classés par priorité.

Rappel du principe : le temps 2 (unification) reprend l'étoile telle quelle,
défauts compris. Un défaut visible dans le fait réuni se corrige donc presque
toujours au temps 1 (hint, assemblage, liaison), pas dans `StarUnification/`.

## Corrigé

### En-tête d'un fait assemblé relié comme une dimension

- **Constat** : le fait magasin, ancré sur `transaction_lines`, recopiait des
  colonnes de `shop_transactions` (l'en-tête du ticket). `shop_transactions`
  restait pourtant une cible de liens : `transaction_id` et même
  `transaction_date` y étaient reliés. La dimension était redondante,
  `transaction_id` entrait dans le grain, et on avait 569 faux doublons
  (le même article deux fois sur un ticket) avec un verdict `needs_review`.
- **Correctif** ([factAssembler.py](kawakiriv3/FactAssembly/factAssembler.py)) :
  une table plus grossière, jointe directement depuis l'ancre, dont
  l'assemblage a recopié des colonnes, est un **en-tête** (`headerTables`).
  Elle n'est plus une cible de liens, et la colonne qui la joint
  (`transaction_id`) devient dégénérée. Le grain devient « grain à la ligne »
  (une note) et le verdict `ok`. Testé dans `testAHeaderTableIsFoldedIntoItsLinesFactNotLinked`.
- **À savoir** : la colonne de document reste dégénérée même si la spec
  d'assemblage la nomme comme clé.

## À faire

### 1. Les colonnes d'un en-tête que le hint n'a pas demandées sont perdues

- **Constat** : selon les runs, le hint des faits demande ou non
  `payment_method` dans le fait magasin. Depuis le correctif ci-dessus,
  l'en-tête n'est plus une cible de liens : une colonne de l'en-tête que le
  hint n'a pas recopiée n'est plus accessible depuis le fait. C'était le cas
  au 3ᵉ run, sans `payment_method`.
- **Correctif proposé** : quand une table est un en-tête, l'assemblage
  recopie automatiquement toutes ses colonnes utiles (sauf sa clé et les
  colonnes techniques, constantes ou vides). Le résultat ne dépend plus du
  LLM.
- **Où** : `FactAssembler.assemble`, après le calcul de `headerTables`.

### 2. Un identifiant de ligne classé « technique » par le LLM disparaît

- **Constat** : le hint des colonnes a classé `web_sales.id` comme
  technique dans certains runs. L'étoile web l'exclut, donc le fait réuni n'a
  pas de numéro de ligne pour le web (`line_id` vide pour ces lignes).
- **Correctif proposé** : dans le classifieur, une colonne unique sur chaque
  ligne et jamais nulle, que le LLM dit technique sans que son nom suive un
  motif technique (`looksTechnical`), reste un identifiant de ligne
  (dégénérée). L'ancien post-traitement avait cette règle (`_isRowIdentifier`),
  supprimée avec lui.
- **Où** : [columnClassifier.py](kawakiriv3/FactAnalysis/columnClassifier.py),
  branche `imposedRole == EXCLUDED`.

### 3. Les noms choisis par le LLM changent d'un run à l'autre

- **Constat** : la structure de l'unification est stable (mêmes sources,
  mêmes colonnes de rapprochement, 750 → 487 clients, 0 orphelin), mais les
  noms varient : `ventes` / `ventes_unifiees`, `nom_produit` / `produit`,
  `shop_sales_lines` / `shop_sales_fact`. Un consommateur en aval qui lit ces
  tables casse.
- **Correctif proposé** : un cache de la proposition d'unification (et du
  hint des faits), comme `LinkVerdictCache` pour les liens, clé = catalogue
  des étoiles + besoin métier. On peut aussi imposer des conventions de
  nommage dans le prompt, mais c'est moins sûr.

### 4. Coût en tokens de l'appel d'unification

- **Constat** : environ 13 000 tokens en entrée, contre environ 5 700 avec
  l'ancien format. On ne sait pas encore si c'est une nouvelle tentative
  (schéma refusé puis corrigé) ou un prompt plus long.
- **Correctif proposé** : renvoyer le nombre de tentatives dans `tokenUsage`
  (`_forced_tool.callValidated`), puis alléger le résumé : ne pas envoyer les
  colonnes des calendriers ni le détail des dimensions extraites qui n'ont que
  leur clé.

### 5. Nom de la colonne de clé par source : `cle_source_<source>`

- **Constat** : quand les clés des sources n'ont pas le même nom (`id` et
  `card_number`) et que le LLM ne les mappe pas (le prompt lui dit qu'il n'a
  pas à le faire), elles sont rangées sous `cle_source`. Cela donne
  `cle_source_web` et `cle_source_magasin`, peu parlants.
- **Correctif proposé** : dériver le nom de l'identifiant unifié
  (`client_unifie_id` → `client_source_web`) dans
  `UnifiedDimensionSpec.sourceKeyTarget`, et le même dans le schéma de
  langgraph_advisor.

### 6. Avertissement de types hétérogènes attendu, mais répété à chaque run

- **Constat** : la clé par source mélange un entier (web) et un texte
  (magasin). L'union la convertit en texte, ce qui est voulu et sans effet
  sur la correspondance (comparée en texte), mais l'avertissement apparaît
  à chaque run.
- **Correctif proposé** : ne pas signaler la colonne de clé par source, ou
  le signaler comme une note.

## Limites assumées (pas de correctif prévu)

- **Dimension conforme par valeur** : un produit aligné sans être unifié
  (`product_name` / `item_name`) donne une dimension par valeur. Un même
  produit écrit différemment selon le canal reste deux lignes. Pour le
  fusionner, il faut l'unifier (une dimension unifiée sur un identifiant
  commun).
- **Calendrier commun** : il porte les attributs standard (année, trimestre,
  mois, jour, jour de semaine). Les colonnes calendaires dérivées qu'une
  étoile avait fondues dans son calendrier (`semaine_iso`) ne sont pas
  reprises.
- **Rôles divergents** : une colonne alignée dont les étoiles ne s'accordent
  pas (clé vers deux tables différentes) devient dégénérée avec un
  avertissement. On corrige l'étoile ou la spec, pas le fait réuni.
- **Ancien format de job** : un job avec `derivedTables` ne passe plus la
  validation. Il faut le réécrire au format `unification`.

## Exploitation

- Le service langgraph_advisor ne recharge pas son code sans `--reload` :
  après une mise à jour, il faut le redémarrer. Sinon
  `/propose-unifications` répond à l'ancien format et kawakiriv3 n'y trouve
  rien (`dimensions` et `facts` vides).
