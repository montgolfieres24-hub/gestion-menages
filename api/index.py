"""
Les Clés du Périgord — Planning des ménages
FastAPI + Airtable + flux iCal Airbnb. Conçu pour Vercel (api/index.py).
"""

import calendar as pycal
import os
import smtplib
import ssl
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from icalendar import Calendar

# ---------------------------------------------------------------- config

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

T_LOGEMENTS = "Logements"
T_EMPLOYEES = "Employées"
T_INDISPOS = "Indisponibilités"
T_MENAGES = "Ménages"

AIRTABLE_API = "https://api.airtable.com/v0"
MAX_EMPLOYEES_PAR_MENAGE = 2

JOURS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
JOURS_COURTS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

app = FastAPI()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


def fr_date(d: date, avec_jour: bool = True) -> str:
    base = f"{d.day} {MOIS[d.month - 1]}"
    return f"{JOURS[d.weekday()]} {base}" if avec_jour else base


templates.env.filters["fr_date"] = fr_date
templates.env.globals["JOURS_COURTS"] = JOURS_COURTS

# ---------------------------------------------------------------- airtable


def _headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}


def at_list(table: str, formula: str = "") -> list[dict]:
    records, offset = [], None
    with httpx.Client(timeout=20) as client:
        while True:
            params = {}
            if formula:
                params["filterByFormula"] = formula
            if offset:
                params["offset"] = offset
            r = client.get(f"{AIRTABLE_API}/{BASE_ID}/{table}",
                           headers=_headers(), params=params)
            r.raise_for_status()
            data = r.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                return records


def at_create(table: str, fields_list: list[dict]):
    with httpx.Client(timeout=20) as client:
        for i in range(0, len(fields_list), 10):
            chunk = [{"fields": f} for f in fields_list[i:i + 10]]
            r = client.post(f"{AIRTABLE_API}/{BASE_ID}/{table}",
                            headers=_headers(),
                            json={"records": chunk, "typecast": True})
            r.raise_for_status()


def at_update(table: str, updates: list[dict]):
    with httpx.Client(timeout=20) as client:
        for i in range(0, len(updates), 10):
            r = client.patch(f"{AIRTABLE_API}/{BASE_ID}/{table}",
                             headers=_headers(),
                             json={"records": updates[i:i + 10],
                                   "typecast": True})
            r.raise_for_status()


def at_delete(table: str, rec_ids: list[str]):
    with httpx.Client(timeout=20) as client:
        for i in range(0, len(rec_ids), 10):
            params = [("records[]", rid) for rid in rec_ids[i:i + 10]]
            r = client.delete(f"{AIRTABLE_API}/{BASE_ID}/{table}",
                              headers=_headers(), params=params)
            r.raise_for_status()


# ---------------------------------------------------------------- airbnb


def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def fetch_reservations(logements: list[dict]) -> list[dict]:
    resas = []
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for lg in logements:
            url = (lg["fields"].get("URL iCal") or "").strip()
            if not url:
                continue
            try:
                r = client.get(url)
                r.raise_for_status()
                cal = Calendar.from_ical(r.text)
            except Exception:
                resas.append({"erreur": True,
                              "logement_id": lg["id"],
                              "logement": lg["fields"].get("Nom", "?")})
                continue
            for ev in cal.walk("VEVENT"):
                summary = str(ev.get("SUMMARY", "")).lower()
                if "not available" in summary or "indisponible" in summary:
                    continue
                try:
                    debut = _as_date(ev.get("DTSTART").dt)
                    fin = _as_date(ev.get("DTEND").dt)
                except Exception:
                    continue
                uid = str(ev.get("UID", "")) or f"{lg['id']}-{debut.isoformat()}"
                resas.append({
                    "erreur": False,
                    "uid": uid,
                    "logement_id": lg["id"],
                    "logement": lg["fields"].get("Nom", "?"),
                    "arrivee": debut,
                    "depart": fin,
                })
    return resas


def sync_menages(resas: list[dict], menages: list[dict]) -> list[dict]:
    """Crée / met à jour / annule les ménages (clé : UID de réservation)."""
    today = date.today()
    valides = [r for r in resas if not r["erreur"]]
    par_uid = {r["uid"]: r for r in valides}
    existants = {m["fields"].get("UID"): m for m in menages if m["fields"].get("UID")}
    arrivees = {(r["logement_id"], r["arrivee"]) for r in valides}

    a_creer, a_maj = [], []
    for uid, r in par_uid.items():
        if r["depart"] < today:
            continue
        meme_jour = (r["logement_id"], r["depart"]) in arrivees
        if uid not in existants:
            a_creer.append({
                "UID": uid,
                "Date": r["depart"].isoformat(),
                "Logement": [r["logement_id"]],
                "Statut": "À planifier",
                "Arrivée le même jour": meme_jour,
            })
        else:
            m = existants[uid]
            f = m["fields"]
            maj = {}
            if f.get("Date") != r["depart"].isoformat():
                maj["Date"] = r["depart"].isoformat()
                if f.get("Statut") == "Envoyé":
                    maj["Statut"] = "Assigné"
            if bool(f.get("Arrivée le même jour")) != meme_jour:
                maj["Arrivée le même jour"] = meme_jour
            if f.get("Statut") == "Annulé":
                maj["Statut"] = "À planifier"
            if maj:
                a_maj.append({"id": m["id"], "fields": maj})

    for uid, m in existants.items():
        f = m["fields"]
        if uid in par_uid or f.get("Statut") in ("Fait", "Annulé"):
            continue
        if (f.get("Date") or "") >= today.isoformat():
            a_maj.append({"id": m["id"], "fields": {"Statut": "Annulé"}})

    if a_creer:
        at_create(T_MENAGES, a_creer)
    if a_maj:
        at_update(T_MENAGES, a_maj)
    if a_creer or a_maj:
        menages = at_list(T_MENAGES)
    return menages


# ---------------------------------------------------------------- métier


def jours_indispo(indispos, employee_id) -> set[str]:
    """Ensemble des jours ISO où l'employée est indisponible."""
    out = set()
    for ind in indispos:
        f = ind["fields"]
        if employee_id not in f.get("Employée", []):
            continue
        du = f.get("Du")
        au = f.get("Au") or du
        if not du:
            continue
        d = date.fromisoformat(du)
        fin = date.fromisoformat(au)
        while d <= fin:
            out.add(d.isoformat())
            d += timedelta(days=1)
    return out


def choix_employees(employees, indispos, jour: date):
    """[(employee, disponible_bool)] pour un jour donné."""
    out = []
    for e in employees:
        if not e["fields"].get("Active", False):
            continue
        dispo = jour.isoformat() not in jours_indispo(indispos, e["id"])
        out.append((e, dispo))
    return out


def construire_dashboard(horizon_jours: int = 60):
    logements = [l for l in at_list(T_LOGEMENTS)
                 if l["fields"].get("Actif", False)]
    employees = at_list(T_EMPLOYEES)
    indispos = at_list(T_INDISPOS)
    menages = at_list(T_MENAGES)

    resas = fetch_reservations(logements)
    erreurs_ical = [r["logement"] for r in resas if r["erreur"]]
    menages = sync_menages(resas, menages)

    today = date.today()
    fin = today + timedelta(days=horizon_jours)
    emp_by_id = {e["id"]: e for e in employees}
    lg_by_id = {l["id"]: l for l in logements}

    jours = defaultdict(lambda: {"arrivees": [], "menages": []})
    for r in resas:
        if r["erreur"]:
            continue
        if today <= r["arrivee"] <= fin:
            jours[r["arrivee"]]["arrivees"].append(r)

    for m in menages:
        f = m["fields"]
        if f.get("Statut") == "Annulé" or not f.get("Date"):
            continue
        d = date.fromisoformat(f["Date"])
        if not (today <= d <= fin):
            continue
        lg_id = (f.get("Logement") or [None])[0]
        emp_ids = f.get("Employée", [])
        jours[d]["menages"].append({
            "id": m["id"],
            "logement": lg_by_id.get(lg_id, {}).get("fields", {}).get("Nom", "?"),
            "employee_ids": emp_ids,
            "employees": [emp_by_id.get(i, {}).get("fields", {}).get("Nom", "?")
                          for i in emp_ids],
            "statut": f.get("Statut", "À planifier"),
            "meme_jour": bool(f.get("Arrivée le même jour")),
            "notes": f.get("Notes", ""),
            "choix": choix_employees(employees, indispos, d),
        })

    timeline = [{"date": d, **jours[d]} for d in sorted(jours)]
    n_a_planifier = sum(1 for j in timeline for m in j["menages"]
                        if m["statut"] == "À planifier")
    n_a_envoyer = sum(1 for j in timeline for m in j["menages"]
                      if m["statut"] == "Assigné")
    return {
        "timeline": timeline,
        "erreurs_ical": erreurs_ical,
        "aucun_logement": not logements,
        "n_a_planifier": n_a_planifier,
        "n_a_envoyer": n_a_envoyer,
    }


def contexte_mois(mois: str | None):
    """'2026-08' → contexte de navigation mensuelle."""
    today = date.today()
    try:
        annee, num = (int(x) for x in (mois or "").split("-"))
        premier = date(annee, num, 1)
    except (ValueError, AttributeError):
        premier = today.replace(day=1)
    prec = (premier - timedelta(days=1)).replace(day=1)
    suiv = (premier + timedelta(days=32)).replace(day=1)
    return {
        "premier": premier,
        "label": f"{MOIS[premier.month - 1].capitalize()} {premier.year}",
        "mois_prec": prec.strftime("%Y-%m"),
        "mois_suiv": suiv.strftime("%Y-%m"),
        "semaines": pycal.Calendar(firstweekday=0)
        .monthdatescalendar(premier.year, premier.month),
    }


# ---------------------------------------------------------------- emails


def envoyer_recaps(semaines: int) -> tuple[int, list[str]]:
    """Un email individuel par employée : uniquement SES créneaux."""
    today = date.today()
    fin = today + timedelta(weeks=semaines)
    employees = {e["id"]: e for e in at_list(T_EMPLOYEES)}
    logements = {l["id"]: l for l in at_list(T_LOGEMENTS)}
    menages = at_list(T_MENAGES)

    par_employee = defaultdict(list)
    for m in menages:
        f = m["fields"]
        if f.get("Statut") not in ("Assigné", "Envoyé"):
            continue
        if not f.get("Date") or not f.get("Employée"):
            continue
        d = date.fromisoformat(f["Date"])
        if today <= d <= fin:
            for emp_id in f["Employée"]:
                par_employee[emp_id].append((d, m))

    envoyes, problemes, statut_maj = 0, [], {}
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        for emp_id, items in par_employee.items():
            emp = employees.get(emp_id)
            if not emp or not emp["fields"].get("Email"):
                nom = emp["fields"].get("Nom", "?") if emp else "?"
                problemes.append(f"{nom} : pas d'adresse email")
                continue
            items.sort(key=lambda x: x[0])
            lignes_txt, lignes_html = [], []
            for d, m in items:
                f = m["fields"]
                lg_id = (f.get("Logement") or [None])[0]
                lg = logements.get(lg_id, {}).get("fields", {}).get("Nom", "?")
                extra = " — nouveaux voyageurs le jour même (ménage prioritaire)" \
                    if f.get("Arrivée le même jour") else ""
                note = f" · {f['Notes']}" if f.get("Notes") else ""
                lignes_txt.append(f"- {fr_date(d)} : {lg}{extra}{note}")
                lignes_html.append(
                    f"<li><strong>{fr_date(d)}</strong> : {lg}"
                    f"{'<em>' + extra + '</em>' if extra else ''}{note}</li>")

            prenom = emp["fields"]["Nom"].split()[0]
            txt = (f"Bonjour {prenom},\n\n"
                   f"Voici tes ménages prévus jusqu'au {fr_date(fin, False)} :\n\n"
                   + "\n".join(lignes_txt)
                   + "\n\nMerci de me signaler tout empêchement."
                     "\n\nBonne journée,\nThibault — Les Clés du Périgord")
            html = (f"<p>Bonjour {prenom},</p>"
                    f"<p>Voici tes ménages prévus jusqu'au "
                    f"<strong>{fr_date(fin, False)}</strong> :</p>"
                    f"<ul>{''.join(lignes_html)}</ul>"
                    "<p>Merci de me signaler tout empêchement.</p>"
                    "<p>Bonne journée,<br>Thibault — Les Clés du Périgord</p>")

            msg = MIMEMultipart("alternative")
            msg["Subject"] = "Planning ménages — semaines à venir"
            msg["From"] = f"Les Clés du Périgord <{GMAIL_USER}>"
            msg["To"] = emp["fields"]["Email"]
            msg.attach(MIMEText(txt, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            try:
                smtp.sendmail(GMAIL_USER, emp["fields"]["Email"], msg.as_string())
                envoyes += 1
                for _, m in items:
                    if m["fields"].get("Statut") == "Assigné":
                        statut_maj[m["id"]] = {"id": m["id"],
                                               "fields": {"Statut": "Envoyé"}}
            except Exception as e:  # noqa: BLE001
                problemes.append(f"{emp['fields']['Nom']} : {e}")
    if statut_maj:
        at_update(T_MENAGES, list(statut_maj.values()))
    return envoyes, problemes


# ---------------------------------------------------------------- diagnostic


PAGE_ERREUR = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Erreur — Les Clés du Périgord</title></head>
<body style="background:#F1EDE3;color:#2A2419;font:15px/1.6 sans-serif;
max-width:640px;margin:0 auto;padding:3rem 1.2rem">
<h1 style="font-weight:600">Aïe, quelque chose a coincé</h1>
<p><strong>{titre}</strong></p><p>{conseil}</p>
<p style="color:#6D6350;font-size:.85rem;word-break:break-all">Détail technique : {detail}</p>
<p><a href="/sante">→ Page de diagnostic</a> · <a href="/">→ Réessayer</a></p>
</body></html>"""


def _expliquer(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return ("Airtable refuse l'accès (jeton invalide ou scopes insuffisants).",
                    "Vérifie AIRTABLE_TOKEN sur Vercel : le jeton doit avoir les scopes "
                    "data.records:read et data.records:write ET l'accès à la base "
                    "Gestion Ménages. Après modification, redéploie l'application.")
        if code == 404:
            return ("Airtable ne trouve pas la base ou une table.",
                    "Vérifie AIRTABLE_BASE_ID (doit être l'identifiant app… de la base "
                    "Gestion Ménages) et que les tables s'appellent bien Logements, "
                    "Employées, Indisponibilités et Ménages.")
        return (f"Airtable a répondu avec une erreur {code}.",
                "Réessaie dans un instant ; si ça persiste, vérifie la configuration.")
    if exc.__class__.__name__ == "TemplateNotFound":
        return ("Les fichiers d'interface (templates) manquent dans le déploiement.",
                "Assure-toi que le dossier api/templates/ du zip a bien été envoyé "
                "sur GitHub, puis redéploie.")
    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
        return ("Impossible de joindre un service externe (Airtable ou Airbnb).",
                "Vérifie les URL iCal dans la table Logements, puis réessaie.")
    return ("Erreur inattendue.",
            "Consulte la page de diagnostic ci-dessous, ou les logs Vercel "
            "(onglet Deployments → ta version → Logs).")


@app.exception_handler(Exception)
async def gestionnaire_erreurs(request: Request, exc: Exception):
    titre, conseil = _expliquer(exc)
    detail = f"{exc.__class__.__name__}: {exc}"
    return HTMLResponse(PAGE_ERREUR.format(titre=titre, conseil=conseil,
                                           detail=detail), status_code=500)


@app.get("/sante", response_class=HTMLResponse)
def sante():
    lignes = []

    def ok(nom, etat, precision=""):
        pastille = "✅" if etat else "❌"
        lignes.append(f"<li>{pastille} {nom}{' — ' + precision if precision else ''}</li>")

    ok("AIRTABLE_TOKEN défini", bool(AIRTABLE_TOKEN))
    ok("AIRTABLE_BASE_ID défini", bool(BASE_ID), BASE_ID or "")
    ok("GMAIL_USER défini", bool(GMAIL_USER))
    ok("GMAIL_APP_PASSWORD défini", bool(GMAIL_APP_PASSWORD))
    ok("APP_PASSWORD défini (recommandé)", bool(APP_PASSWORD))
    tpl = Path(__file__).resolve().parent / "templates"
    ok("Dossier templates présent", tpl.is_dir(),
       ", ".join(sorted(p.name for p in tpl.glob("*.html"))) if tpl.is_dir() else "")

    if AIRTABLE_TOKEN and BASE_ID:
        for table in (T_LOGEMENTS, T_EMPLOYEES, T_INDISPOS, T_MENAGES):
            try:
                n = len(at_list(table))
                ok(f"Table « {table} » accessible", True, f"{n} enregistrement(s)")
            except httpx.HTTPStatusError as e:
                ok(f"Table « {table} » accessible", False,
                   f"Airtable a répondu {e.response.status_code}")
            except Exception as e:  # noqa: BLE001
                ok(f"Table « {table} » accessible", False, str(e))

    return HTMLResponse(
        "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Diagnostic</title></head>"
        "<body style='background:#F1EDE3;color:#2A2419;font:15px/1.7 sans-serif;"
        "max-width:640px;margin:0 auto;padding:3rem 1.2rem'>"
        "<h1 style='font-weight:600'>Diagnostic</h1><ul style='list-style:none;padding:0'>"
        + "".join(lignes) + "</ul><p><a href='/'>→ Retour à l'application</a></p></body></html>")


# ---------------------------------------------------------------- accès


def connecte(request: Request) -> bool:
    return not APP_PASSWORD or request.cookies.get("acces") == APP_PASSWORD


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, erreur: int = 0):
    return templates.TemplateResponse(request, "login.html", {"erreur": erreur})


@app.post("/login")
def login(mot_de_passe: str = Form(...)):
    if APP_PASSWORD and mot_de_passe == APP_PASSWORD:
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("acces", APP_PASSWORD, httponly=True,
                        max_age=60 * 60 * 24 * 90)
        return resp
    return RedirectResponse("/login?erreur=1", status_code=303)


# ---------------------------------------------------------------- routes


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, message: str = "", alerte: str = ""):
    if not connecte(request):
        return RedirectResponse("/login", status_code=303)
    if not AIRTABLE_TOKEN or not BASE_ID:
        return templates.TemplateResponse(request, "setup.html", {})
    data = construire_dashboard()
    return templates.TemplateResponse(
        request, "dashboard.html",
        {**data, "today": date.today(), "message": message, "alerte": alerte,
         "onglet": "dashboard", "max_emp": MAX_EMPLOYEES_PAR_MENAGE,
         "email_ok": bool(GMAIL_USER and GMAIL_APP_PASSWORD)})


@app.post("/affecter")
def affecter(request: Request, menage_id: str = Form(...),
             employee1_id: str = Form(""), employee2_id: str = Form("")):
    if not connecte(request):
        return RedirectResponse("/login", status_code=303)
    ids = []
    for i in (employee1_id, employee2_id):
        if i and i not in ids:
            ids.append(i)
    fields = ({"Employée": ids, "Statut": "Assigné"} if ids else
              {"Employée": [], "Statut": "À planifier"})
    at_update(T_MENAGES, [{"id": menage_id, "fields": fields}])
    return RedirectResponse("/", status_code=303)


@app.post("/statut")
def statut(request: Request, menage_id: str = Form(...),
           nouveau: str = Form(...)):
    if not connecte(request):
        return RedirectResponse("/login", status_code=303)
    if nouveau in ("À planifier", "Assigné", "Envoyé", "Fait", "Annulé"):
        at_update(T_MENAGES, [{"id": menage_id, "fields": {"Statut": nouveau}}])
    return RedirectResponse("/", status_code=303)


@app.post("/envoyer-recaps")
def recaps(request: Request, semaines: int = Form(2)):
    if not connecte(request):
        return RedirectResponse("/login", status_code=303)
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return RedirectResponse(
            "/?alerte=Configure GMAIL_USER et GMAIL_APP_PASSWORD sur Vercel",
            status_code=303)
    envoyes, problemes = envoyer_recaps(max(1, min(semaines, 8)))
    msg = f"{envoyes} récapitulatif{'s' if envoyes > 1 else ''} envoyé{'s' if envoyes > 1 else ''}"
    if problemes:
        return RedirectResponse(
            f"/?message={msg}&alerte={' · '.join(problemes)}", status_code=303)
    return RedirectResponse(f"/?message={msg}", status_code=303)


# ------------------------------------------------ onglet disponibilités


@app.get("/disponibilites", response_class=HTMLResponse)
def disponibilites(request: Request, mois: str = ""):
    if not connecte(request):
        return RedirectResponse("/login", status_code=303)
    ctx = contexte_mois(mois)
    employees = [e for e in at_list(T_EMPLOYEES)
                 if e["fields"].get("Active", False)]
    indispos = at_list(T_INDISPOS)
    lignes = [{"employee": e,
               "indispo": jours_indispo(indispos, e["id"])}
              for e in employees]
    jours_du_mois = [d for sem in ctx["semaines"] for d in sem
                     if d.month == ctx["premier"].month]
    return templates.TemplateResponse(
        request, "disponibilites.html",
        {**ctx, "lignes": lignes, "jours_du_mois": jours_du_mois,
         "today": date.today(), "onglet": "dispos",
         "aucune_employee": not employees})


@app.post("/disponibilite")
def toggle_disponibilite(request: Request, employee_id: str = Form(...),
                         jour: str = Form(...), mois: str = Form("")):
    if not connecte(request):
        return RedirectResponse("/login", status_code=303)
    d = date.fromisoformat(jour)
    indispos = at_list(T_INDISPOS)
    couvrants = []
    for ind in indispos:
        f = ind["fields"]
        if employee_id not in f.get("Employée", []):
            continue
        du = f.get("Du")
        au = f.get("Au") or du
        if du and du <= jour <= au:
            couvrants.append((ind, du, au))

    if not couvrants:  # jour libre → le marquer indisponible
        at_create(T_INDISPOS, [{"Motif": "Indisponible",
                                "Employée": [employee_id],
                                "Du": jour, "Au": jour}])
    else:  # jour indisponible → le libérer (suppression / découpe des périodes)
        for ind, du, au in couvrants:
            if du == au:
                at_delete(T_INDISPOS, [ind["id"]])
            elif du == jour:
                at_update(T_INDISPOS, [{"id": ind["id"], "fields": {
                    "Du": (d + timedelta(days=1)).isoformat()}}])
            elif au == jour:
                at_update(T_INDISPOS, [{"id": ind["id"], "fields": {
                    "Au": (d - timedelta(days=1)).isoformat()}}])
            else:  # découpe en deux périodes autour du jour libéré
                at_update(T_INDISPOS, [{"id": ind["id"], "fields": {
                    "Au": (d - timedelta(days=1)).isoformat()}}])
                at_create(T_INDISPOS, [{
                    "Motif": ind["fields"].get("Motif", "Indisponible"),
                    "Employée": [employee_id],
                    "Du": (d + timedelta(days=1)).isoformat(), "Au": au}])
    return RedirectResponse(f"/disponibilites?mois={mois}", status_code=303)


# ------------------------------------------------ onglet vue mensuelle


@app.get("/planning", response_class=HTMLResponse)
def planning(request: Request, mois: str = ""):
    if not connecte(request):
        return RedirectResponse("/login", status_code=303)
    ctx = contexte_mois(mois)
    premier = ctx["premier"]
    dernier = (premier + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    logements = [l for l in at_list(T_LOGEMENTS)
                 if l["fields"].get("Actif", False)]
    employees = {e["id"]: e for e in at_list(T_EMPLOYEES)}
    lg_by_id = {l["id"]: l for l in logements}
    menages = at_list(T_MENAGES)
    resas = fetch_reservations(logements)

    evenements = defaultdict(lambda: {"menages": [], "arrivees": []})
    for r in resas:
        if not r["erreur"] and premier <= r["arrivee"] <= dernier:
            evenements[r["arrivee"]]["arrivees"].append(r["logement"])
    for m in menages:
        f = m["fields"]
        if f.get("Statut") == "Annulé" or not f.get("Date"):
            continue
        d = date.fromisoformat(f["Date"])
        if not (premier <= d <= dernier):
            continue
        lg_id = (f.get("Logement") or [None])[0]
        noms = [employees.get(i, {}).get("fields", {}).get("Nom", "?")
                for i in f.get("Employée", [])]
        evenements[d]["menages"].append({
            "logement": lg_by_id.get(lg_id, {}).get("fields", {}).get("Nom", "?"),
            "employees": noms,
            "statut": f.get("Statut"),
            "meme_jour": bool(f.get("Arrivée le même jour")),
        })
    return templates.TemplateResponse(
        request, "planning.html",
        {**ctx, "evenements": evenements, "today": date.today(),
         "onglet": "planning"})
