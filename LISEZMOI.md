# BetScope – Analyseur de paris sportifs

## Obtenir le .exe (Windows)

1. Installe Python depuis https://www.python.org/downloads/ (coche **Add python.exe to PATH**).
2. Double-clique sur **build_exe.bat**.
3. Ton logiciel est créé dans `dist\BetScope.exe`. Tu peux le copier où tu veux.

Autre option : mets ce dossier dans un dépôt GitHub. Le workflow `.github/workflows/build.yml`
fabrique le .exe tout seul (onglet *Actions*, ou en poussant un tag `v1.0.0` qui crée une *Release*).

## Brancher les vraies cotes

1. Crée une clé gratuite sur https://the-odds-api.com (500 crédits/mois).
2. Dans BetScope → **Paramètres**, colle la clé, coche tes compétitions, mets tes bookmakers, puis **Enregistrer**.

Sans clé, l'appli tourne en **mode démo** (matchs fictifs) pour que tu voies comment elle fonctionne.

L'appli se met à jour toute seule à l'intervalle choisi. Chaque mise à jour coûte des crédits
(compétitions × régions × marchés) et l'onglet Paramètres affiche le coût mensuel estimé.
Pour du quasi temps réel (toutes les 5–10 min), il faut un abonnement payant The Odds API.

## Comment l'analyse marche

- **Proba réelle** : on retire la marge de chaque bookmaker, puis on fait la moyenne du marché
  (Pinnacle et les exchanges, les plus précis, comptent davantage).
- **Value** = proba réelle × cote − 1. Si c'est positif, le pari est rentable *en moyenne*.
- **Combinés** : uniquement des value bets sur des matchs différents, chez **un même bookmaker**
  (sinon le combiné serait impossible à jouer).
- **Mon budget** : répartit ta mise selon le critère de Kelly, puis simule 20 000 fois le résultat
  (chance de gagner, de tout perdre, gain moyen espéré).
- **⚠** : value anormalement haute, souvent une cote périmée. Vérifie-la avant de parier.

## Important

Aucun outil ne garantit des gains. Un value bet perd souvent, il gagne seulement sur la durée.
Les bookmakers limitent parfois les comptes qui gagnent régulièrement.
Ne mise que ce que tu peux te permettre de perdre. 18+. Aide : Joueurs Info Service, 09 74 75 13 13.
