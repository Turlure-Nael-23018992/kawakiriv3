# Dataset `jde_liens`

Un petit registre de créances **synthétique**, en forme de JD Edwards
(aucune donnée réelle). Il est régénéré par :

```bash
py -3.12 scripts/generate_jde_liens_dataset.py
```

Il est construit pour l'**arbitrage LLM des liens**. Chez JDE, une colonne
s'écrit « préfixe de table (2 lettres) + nom de champ » : `RPAN8` (F03B11)
et `ABAN8` (F0101) sont le même numéro d'adresse. **Aucun nom ne concorde
jamais**, donc aucune règle déterministe ne peut accepter ces liens.

## Tables

| Table | Lignes | Rôle |
|---|---|---|
| `f03b11` | 2 617 | registre des créances : une ligne par échéance de facture |
| `f0101` | 250 | carnet d'adresses (clients), `ABAN8` dispersés entre 100 000 et 999 999 |
| `f0010` | 5 | sociétés, `CCCO` de 1 à 5 |
| `f0006` | 7 | centres de coût, `MCMCU` codés sur 12 caractères cadrés à droite |

## Les liens et ce qu'on en attend

| Lien | Vrai ? | Ce qu'il teste |
|---|---|---|
| `f03b11.RPAN8 -> f0101.ABAN8` | oui | le client (champ `AN8`) |
| `f03b11.RPCO -> f0010.CCCO` | oui | la société (champ `CO`) |
| `f03b11.RPKCO -> f0010.CCCO` | oui | la société du document (champ `KCO`) |
| `f03b11.RPMCU -> f0006.MCMCU` | oui | le centre de coût (champ `MCU`), une clé texte |
| `f0006.MCCO -> f0010.CCCO` | oui | la société du centre de coût |
| `f03b11.RPSFX -> f0010.CCCO` | **non** | le piège : un numéro d'échéance (1 à 3), dans la plage des sociétés par hasard, à 100 % de couverture |

Les six liens sont tous « douteux » pour les règles déterministes (noms
discordants, forte couverture) : ils partent tous à l'arbitrage, en un seul
appel.

## Runs réels (2026-10-28)

Job : `job_jde_liens.json`. Sorties dans [resultats/](resultats/).

- **Run 1** : arbitrage en un appel (2 714 tokens en entrée, 852 en
  sortie). **5 verdicts justes sur 6** :
  - acceptés : `AN8`, `CO`, `MCU`, `MCCO`, chacun justifié par le champ
    JDE commun ;
  - le piège `RPSFX` est refusé (« compteur séquentiel, petite plage ») ;
  - **erreur** : `RPKCO -> CCCO` est refusé, jugé redondant avec `RPCO`.
    C'est pourtant un vrai lien. Pour le corriger :
    `"declaredLinks": ["f03b11.RPKCO -> f0010.CCCO"]` dans le job.
- **Run 2** : aucun appel d'arbitrage, tous les verdicts viennent du
  cache (`output/jde_liens_link_verdicts.json`). Le graphe et l'étoile
  sont identiques au run 1.

L'étoile : `RPAN8`, `RPCO` et `RPMCU` sont des clés étrangères vers
`f0101`, `f0010` et `f0006`. Le verdict est **ok**. Sans arbitrage (fait
imposé, `columnHint: false`), seul `RPMCU`, une clé texte, est lié :
`RPAN8` et `RPCO` sont des entiers que rien ne nomme comme des clés, ils
deviennent des dimensions générées au lieu de pointer vers le carnet
d'adresses et les sociétés.

Le jugement du LLM n'est pas déterministe ; le cache le fige d'un run à
l'autre, et le job a toujours le dernier mot.
