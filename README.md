# Les Clés du Périgord — Planning des ménages

Webapp FastAPI qui lit les calendriers iCal Airbnb de tes logements, crée automatiquement un ménage à chaque départ, te laisse affecter tes employées selon leurs disponibilités, et leur envoie un récapitulatif par email en un clic.

## 1. La base Airtable

La base « Gestion Ménages » contient 4 tables (créées par Claude) :

| Table | Rôle |
|---|---|
| **Logements** | Nom, URL iCal Airbnb, case « Actif » |
| **Employées** | Nom, Email, Téléphone, case « Active » |
| **Indisponibilités** | Employée + période Du/Au (congés, absences) |
| **Ménages** | Générée automatiquement par l'app — tu peux y ajouter des Notes |

Récupérer l'URL iCal d'un logement Airbnb : **Calendrier → Disponibilité → Synchronisation des calendriers → Exporter le calendrier**. Colle l'URL dans la table Logements.

## 2. Déployer sur Vercel (sans terminal)

1. Sur github.com : **New repository** → nom `gestion-menages` → **uploading an existing file** → glisse tout le contenu du zip (les dossiers `api/` et `templates/`, plus `requirements.txt` et `vercel.json`).
2. Sur vercel.com : **Add New → Project** → importe le dépôt → **Deploy**.
3. Dans le projet Vercel : **Settings → Environment Variables**, ajoute :

| Variable | Valeur |
|---|---|
| `AIRTABLE_TOKEN` | Jeton créé sur airtable.com/create/tokens — scopes `data.records:read` + `data.records:write`, accès à la base Gestion Ménages |
| `AIRTABLE_BASE_ID` | L'identifiant `app…` de la base (visible dans l'URL Airtable) |
| `GMAIL_USER` | Ton adresse Gmail d'envoi |
| `GMAIL_APP_PASSWORD` | Mot de passe d'application : myaccount.google.com/apppasswords (nécessite la validation en 2 étapes) |
| `APP_PASSWORD` | Mot de passe d'accès à l'app (recommandé — l'URL est publique) |

4. **Deployments → ⋯ → Redeploy** pour prendre en compte les variables.

## 3. Les trois onglets

- **Tableau de bord** : arrivées/départs à venir ; jusqu'à **deux employées par ménage** (deux menus déroulants).
- **Disponibilités** : grille mensuelle de l'équipe ; **un clic sur une case bascule** le jour entre disponible et indisponible (les périodes saisies dans Airtable sont respectées et découpées automatiquement si tu libères un jour au milieu).
- **Vue mensuelle** : calendrier du mois avec tous les ménages (et qui les fait) et les arrivées de voyageurs.

## 4. Utilisation quotidienne

- Le tableau de bord se synchronise avec Airbnb **à chaque ouverture de la page** (Airbnb rafraîchit ses flux iCal environ toutes les 2 à 3 heures).
- Chaque départ apparaît comme un ménage « À planifier » ; choisis l'employée dans le menu (celles en congé sont marquées « indisponible »).
- Le badge orange « Arrivée le jour même » signale les ménages prioritaires.
- Le bouton en bas envoie à chaque employée **son récapitulatif individuel** (uniquement ses propres créneaux, sans les attributions des autres) ; leur statut passe à « Envoyé ». Si une date change ensuite côté Airbnb, le statut redescend à « Assigné » pour te rappeler de renvoyer le récap.
- « Marquer fait » archive le ménage.

## Statuts

`À planifier` → `Assigné` → `Envoyé` → `Fait` (et `Annulé` si la réservation disparaît du flux Airbnb).
