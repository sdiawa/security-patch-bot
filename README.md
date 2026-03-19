🔐 Security Patch Bot – Guide d’utilisation (Entreprise)

📌 Objectif

Le Security Patch Bot permet d’automatiser la mise à jour des :
	•	dépendances Helm (Chart.yaml)
	•	images Docker (values.yaml)

afin de :
	•	corriger les vulnérabilités
	•	aligner les versions
	•	standardiser les déploiements

⸻

⚙️ Fonctionnement global

Le bot :
	1.	🔍 Scanne les projets GitLab
	2.	📦 Identifie les versions non conformes
	3.	🛠 Applique les règles définies dans config.yaml
	4.	📄 Génère un rapport (report.md)
	5.	🔀 Crée une Merge Request (optionnel)

Architecture
GitLab CI Pipeline
      ↓
patch_bot.py
      ↓
Scan repo (Chart + values)
      ↓
Patch versions (policy)
      ↓
Generate report.md
      ↓
Create MR (apply mode)

📂 Fichiers principaux

Fichier            Description
config.yaml        Politique de patch (versions autorisées)
patch_bot.py       Script principal
.gitlab-ci.yml.    Pipeline CI
report.md.         Rapport généré

Utilisation:

1️⃣ Lancer un scan (dry-run)
Pipeline:
security-report

➡️ Mode lecture seule
➡️ Aucun changement effectué
➡️ Génère un rapport

2️⃣ Appliquer les patchs

Pipeline :
security-apply

➡️ Crée une branche
➡️ Commit les changements
➡️ Ouvre une Merge Request

Paramètres du pipeline
Variable       Description.      Exemple
SCOPE.          Portée.         group / project
GROUP_ID.       ID du groupe.        25
PROJECT_ID.     ID projet.         80720
PROJECT_PATH.   Chemin projet.   dsk-lab/api
ENVS.           Environnements  dev / qua / prod
TARGET_BRANCH.  Branche cible.  master / roks
BRANCH_PREFIX.  Préfixe MR.     sec/patch

Environnements supportés
dev
int
qua
prod
qualiso
prd

Exemple :
ENVS=dev

Exemple de rapport

# 🔐 Security Patch Report

Project: retail/backend

## Changes

File: dev/Chart.yaml
- backend: 1.0.9 → 1.0.10

File: dev/values.yaml
- vault: 1.15 → 1.21

---

## Summary

Projects scanned: 10
Projects changed: 2
Files updated: 4

Exemple de Merge Request
Security patches (20260316)

Contenu :
Project: retail/backend

Files changed:
- dev/Chart.yaml
- dev/values.yaml

Changes:
backend 1.0.9 → 1.0.10
vault 1.15 → 1.21

Règles de patch

Helm dependencies:
backend: "1.0.10"
batch: "1.1.14"
spark: "3.1.0"

➡️ Le bot aligne automatiquement :
	•	dependency version
	•	chart version

Images Docker:
vault:
  tag: "1.21.2"

➡️ Le bot met à jour :
image:
  repository: hashicorp/vault
  tag: 1.21.2

Gestion des tokens

Chaque utilisateur peut utiliser son propre token :
GITLAB_TOKEN=xxxx

➡️ Permet :
	•	accès à ses projets
	•	traçabilité
	•	sécurité


⚠️ Cas d’erreur fréquents

❌ GROUP_ID vide

Erreur :
TypeError: int() argument must be...
✔ Solution :
	•	remplir GROUP_ID
	•	ou utiliser config.yaml

❌ Aucun fichier trouvé
no matching files

✔ Vérifier :
	•	chart_globs
	•	values_globs
	•	structure repo

❌ Aucun changement
NO-CHANGE

✔ Normal → versions déjà conformes

Bonnes pratiques

✔ utiliser d’abord security-report
✔ valider avant security-apply
✔ limiter scope (project) pour test
✔ utiliser dev avant prod


