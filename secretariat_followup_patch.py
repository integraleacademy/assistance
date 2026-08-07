"""Correctifs du mail récapitulatif envoyé à la fin du parcours /secretariat."""

import datetime
import html
import json
import os
import re
import unicodedata
import uuid

import pytz
from flask import has_request_context, url_for


PUBLIC_BASE_URL = "https://assistance-alw9.onrender.com"


def _text(value):
    return str(value or "").strip()


def _norm(value):
    value = unicodedata.normalize("NFKD", _text(value))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _yes(value):
    return _text(value).upper() in {"OUI", "YES", "TRUE", "1"}


def _euros(value):
    try:
        amount = int(float(str(value).replace(" ", "").replace(",", ".")))
    except (TypeError, ValueError):
        return ""
    return f"{amount:,}".replace(",", " ") + " €"


def register_secretariat_followup_patch(app_module):
    if getattr(app_module, "_secretariat_followup_patch_registered", False):
        return
    app_module._secretariat_followup_patch_registered = True

    original_first_name = app_module._crm_format_first_name
    original_send = app_module._send_secretariat_information_messages

    def first_name(value):
        formatted = original_first_name(value)
        return re.sub(r"(?i)(?<!\w)clement(?!\w)", "Clément", formatted)

    def split_session(raw_value):
        raw = _text(raw_value)
        centre_part, session = "", raw
        if " — " in raw:
            centre_part, session = raw.split(" — ", 1)
        probe = _norm(centre_part or raw)
        if "cote d azur" in probe or "cote azur" in probe:
            return (
                "cote_azur",
                app_module.FORMATION_CENTRES.get("cote_azur", "Intégrale Academy Côte d’Azur"),
                "Puget-sur-Argens",
                session.strip(),
            )
        if "auvergne" in probe or "aurillac" in probe:
            return (
                "auvergne",
                app_module.FORMATION_CENTRES.get("auvergne", "Intégrale Academy Terres d’Auvergne"),
                "Aurillac",
                session.strip(),
            )
        if "paris" in probe:
            return (
                "paris",
                app_module.FORMATION_CENTRES.get("paris", "Intégrale Academy Paris"),
                "Paris",
                session.strip(),
            )
        return "", centre_part.strip(), "", session.strip()

    def objective(code, config):
        if code == "APS":
            return (
                "Votre objectif est de suivre le parcours complet permettant d’obtenir le TFP APS, "
                "puis de demander la carte professionnelle indispensable pour exercer dans le métier "
                "d’agent de surveillance humaine et de gardiennage."
            )
        if code == "A3P":
            return (
                "Votre objectif est de suivre le parcours complet de protection physique des personnes, "
                "puis d’effectuer les démarches nécessaires auprès du CNAPS pour exercer cette activité."
            )
        certification = _text(config.get("certification"))
        purpose = _text(config.get("purpose"))
        if certification:
            return f"Votre objectif est de suivre ce parcours afin de préparer {certification[0].lower() + certification[1:]}."
        if purpose:
            return f"Votre objectif est de suivre ce parcours pour {purpose[0].lower() + purpose[1:]}"
        return "Votre objectif est d’avancer vers la certification et le métier visés avec l’accompagnement de notre équipe."

    def training_price(code, entry):
        try:
            return int(app_module.get_formation_tarif(code, entry))
        except Exception:
            return int(app_module.PLAN_TARIFS.get(code, 0) or 0)

    def fallback(entry):
        code = _text(entry.get("formation")).upper()
        config = app_module._secretariat_formation_config(code)
        _, centre, city, session = split_session(entry.get("formation_date_souhaitee"))
        session_text = re.sub(r"^(Du|Le)\s+", lambda match: match.group(0).lower(), session, count=1)
        if session:
            location = f" dans notre centre de formation {centre}" if centre else ""
            if location and city:
                location += f" à {city}"
            first = f"Vous souhaitez intégrer la session organisée {session_text}{location}. {objective(code, config)}"
        else:
            first = f"Vous souhaitez avancer sur votre projet de formation {config.get('label')}. {objective(code, config)}"

        paragraphs = [first]
        cpf = app_module._parse_cpf_value(entry.get("cpf_montant"))
        price = training_price(code, entry)
        financing = []
        if cpf:
            if price and cpf >= price:
                financing.append(
                    f"Vous nous avez indiqué disposer de {_euros(cpf)} sur votre compte CPF. "
                    f"Ce montant couvre le tarif de la formation, fixé à {_euros(price)} TTC."
                )
            elif price:
                financing.append(
                    f"Vous nous avez indiqué disposer de {_euros(cpf)} sur votre compte CPF. "
                    f"Ce montant peut financer une partie du tarif de la formation, fixé à {_euros(price)} TTC."
                )
            else:
                financing.append(f"Vous nous avez indiqué disposer de {_euros(cpf)} sur votre compte CPF.")
        if _yes(entry.get("france_travail")):
            status = _norm(entry.get("france_travail_status"))
            if status in {"submitted", "transmitted", "transmise", "deposee", "depose"}:
                financing.append("Votre demande de financement auprès de France Travail a été transmise et reste soumise à la décision de l’organisme.")
            elif status in {"pending", "en cours", "en attente"}:
                financing.append("Votre demande de financement auprès de France Travail est indiquée comme étant en cours d’instruction.")
            elif status in {"approved", "accepted", "acceptee", "accepte"}:
                financing.append("Votre demande de financement auprès de France Travail est indiquée comme acceptée.")
            else:
                financing.append("Vous souhaitez également connaître les possibilités de prise en charge par France Travail.")
        if _yes(entry.get("ft_refus_ok")) or _yes(entry.get("financement_perso")):
            if _yes(entry.get("france_travail")):
                financing.append("Vous avez prévu un financement personnel si cette solution n’aboutit pas.")
            elif price and cpf < price:
                financing.append("Vous avez prévu un financement personnel pour compléter le reste à charge.")
            else:
                financing.append("Vous avez également indiqué qu’un financement personnel restait possible si nécessaire.")
        if financing:
            paragraphs.append(" ".join(financing))

        administrative = []
        identity = _text(entry.get("identite_numerique")).upper()
        if identity == "OUI":
            administrative.append("Votre Identité Numérique La Poste est déjà créée.")
        elif identity == "NON":
            administrative.append("Votre Identité Numérique La Poste n’est pas encore créée ; elle sera nécessaire pour mobiliser votre CPF.")
        if code == "APS":
            if _yes(entry.get("cnaps_ok")):
                administrative.append("Vous disposez déjà d’une carte professionnelle CNAPS en cours de validité.")
            else:
                administrative.append(
                    "Vous ne disposez pas encore d’une carte professionnelle valide ; cette situation est normale avant l’entrée en formation "
                    "et notre équipe pourra vous accompagner pour les démarches d’autorisation auprès du CNAPS."
                )
        elif code == "A3P":
            if _yes(entry.get("cnaps_ok")):
                administrative.append("Votre carte professionnelle CNAPS est indiquée comme valide ; notre équipe vérifiera avec vous les justificatifs nécessaires.")
            else:
                administrative.append("Notre équipe vérifiera avec vous la démarche CNAPS adaptée à votre situation avant l’entrée en formation A3P.")
        if administrative:
            paragraphs.append(" ".join(administrative))
        if len(paragraphs) == 1:
            paragraphs.append("Notre équipe reste à votre disposition pour vérifier avec vous les prérequis, le financement et les documents nécessaires avant l’inscription.")

        steps = ["Consulter le dossier de présentation et l’ensemble des prochaines dates."]
        if session:
            steps.append("Confirmer avec notre équipe la session que vous avez retenue.")
        if _yes(entry.get("devis")):
            steps.append("Consulter votre devis personnalisé généré à partir des informations communiquées.")
        if code in {"APS", "A3P"} and not _yes(entry.get("cnaps_ok")):
            steps.append("Finaliser avec notre équipe les démarches CNAPS nécessaires avant l’entrée en formation.")
        return {"summary_paragraphs": paragraphs[:4], "financing_message": "", "cnaps_message": "", "next_steps": steps[:4]}

    def validate_ai(raw, backup, displayed_first_name, entry):
        try:
            if isinstance(raw, str):
                raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw, flags=re.I)
                start, end = raw.find("{"), raw.rfind("}")
                raw = json.loads(raw[start : end + 1])
            paragraphs = [_text(item) for item in raw.get("summary_paragraphs", []) if _text(item)]
            steps = [_text(item) for item in raw.get("next_steps", []) if _text(item)]
            if not 2 <= len(paragraphs) <= 4 or not 2 <= len(steps) <= 4:
                raise ValueError("structure")
            all_text = paragraphs + steps
            forbidden = (
                "bonjour", "le candidat", "la candidate", "le prospect", "la personne souhaite",
                "il souhaite", "elle souhaite", "il devra", "elle devra", "cassandre menard", "<",
            )
            if any(paragraph.lower().lstrip().startswith("merci") for paragraph in paragraphs):
                raise ValueError("duplicate thanks")
            if any(any(token in item.lower() for token in forbidden) for item in all_text):
                raise ValueError("forbidden wording")
            normalized_name = _norm(displayed_first_name)
            if normalized_name and any(re.search(rf"(?<!\w){re.escape(normalized_name)}(?!\w)", _norm(item)) for item in all_text):
                raise ValueError("name repeated")
            joined = " ".join(paragraphs)
            normalized_joined = _norm(joined)
            _, _, _, selected = split_session(entry.get("formation_date_souhaitee"))
            if selected and _norm(selected)[:18] not in normalized_joined:
                raise ValueError("selected session missing")
            cpf_digits = re.sub(r"\D", "", _text(entry.get("cpf_montant")))
            if cpf_digits and cpf_digits not in re.sub(r"\D", "", joined):
                raise ValueError("cpf missing")
            if _yes(entry.get("france_travail")) and "france travail" not in normalized_joined:
                raise ValueError("France Travail missing")
            if _text(entry.get("identite_numerique")) and "identite numerique" not in normalized_joined:
                raise ValueError("identity missing")
            code = _text(entry.get("formation")).upper()
            if code in {"APS", "A3P"} and not any(token in normalized_joined for token in ("cnaps", "carte professionnelle", "autorisation")):
                raise ValueError("CNAPS missing")
            if not _norm(entry.get("france_travail_status")) and re.search(
                r"demande.{0,35}(transmise|déposée|en cours|en attente|validée|acceptée)", joined, re.I
            ):
                raise ValueError("invented France Travail status")
            if code not in {"APS", "A3P"}:
                steps = [step for step in steps if not re.search(r"CNAPS|carte professionnelle", step, re.I)]
            return {"summary_paragraphs": paragraphs, "financing_message": "", "cnaps_message": "", "next_steps": steps}
        except Exception:
            return backup

    def upcoming_sessions(code):
        try:
            sessions = app_module.get_upcoming_formation_sessions(app_module.load_data())
        except Exception:
            return []
        result = []
        for centre_code, centre_label in app_module.FORMATION_CENTRES.items():
            rows = [row for row in sessions.get(centre_code, {}).get(code, []) if _text(row.get("label"))]
            if rows:
                result.append({"centre_label": centre_label, "sessions": rows})
        return result

    def quote_url(token):
        if has_request_context():
            try:
                return url_for("plan_public", token=token, _external=True)
            except Exception:
                pass
        return f"{os.getenv('PUBLIC_BASE_URL', PUBLIC_BASE_URL).rstrip('/')}/plan/{token}"

    def ensure_quote(data, entry, contact):
        if not _yes(entry.get("devis")):
            entry.pop("devis_url", None)
            return None
        requests_list = data.setdefault("demandes", [])
        quote_id = _text(entry.get("devis_id") or contact.get("source_devis_id"))
        quote = next((row for row in requests_list if row.get("id") == quote_id), None) if quote_id else None
        if quote is None:
            quote = next(
                (row for row in requests_list if row.get("motif") == "Demande de devis détaillé" and row.get("source_secretariat_id") == entry.get("id")),
                None,
            )
        created = quote is None
        if created:
            quote = {"id": str(uuid.uuid4())}
            requests_list.append(quote)
        token = _text(quote.get("token_plan")) or uuid.uuid4().hex
        centre_code, centre_label, city, session = split_session(entry.get("formation_date_souhaitee"))
        details = {
            "nom": _text(contact.get("nom")), "prenom": first_name(contact.get("prenom")),
            "telephone": _text(entry.get("telephone")), "mail": _text(entry.get("email")),
            "formation": _text(entry.get("formation")), "dates": session, "centre": centre_code,
            "date_examen": app_module._parse_exam_date_from_dates_txt(session) if session else "",
            "ssiap_secourisme_valide": _text(entry.get("ssiap_secourisme_valide")),
            "cpf_consulte": _text(entry.get("cpf_consulte")), "cpf_montant": _text(entry.get("cpf_montant") or "0"),
            "france_travail": _text(entry.get("france_travail") or "NON"), "ft_refus_ok": _text(entry.get("ft_refus_ok")),
            "financement_perso": _text(entry.get("financement_perso")), "identite_numerique": _text(entry.get("identite_numerique") or "NON"),
            "cnaps_ok": _text(entry.get("cnaps_ok")), "source_secretariat_id": entry.get("id"),
        }
        now = datetime.datetime.now(pytz.timezone("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
        quote.update({
            "token_plan": token, "source_secretariat_id": entry.get("id"), "nom": details["nom"], "prenom": details["prenom"],
            "telephone": details["telephone"], "mail": details["mail"], "motif": "Demande de devis détaillé",
            "details": json.dumps(details, ensure_ascii=False), "date": quote.get("date") or now,
            "statut": quote.get("statut") or "Non traité", "attribution": quote.get("attribution") or "",
            "commentaire": quote.get("commentaire") or "", "commentaire_admin": quote.get("commentaire_admin") or "",
            "mail_confirme": quote.get("mail_confirme") or "", "mail_erreur": quote.get("mail_erreur") or "",
            "mail_contenu": quote.get("mail_contenu") or "", "mail_html": quote.get("mail_html") or "",
            "pieces_jointes": quote.get("pieces_jointes") or [], "reponses": quote.get("reponses") or [],
            "is_doublon": bool(quote.get("is_doublon", False)), "rappel_date": quote.get("rappel_date") or "",
            "plage": quote.get("plage") or "", "statut_devis": quote.get("statut_devis") or "A envoyer",
            "notation_interne": quote.get("notation_interne") or "", "echeancier_manuel": quote.get("echeancier_manuel") or [],
            "pdf_path": quote.get("pdf_path") or "", "source": "assistant-secretariat",
        })
        public_url = quote_url(token)
        entry.update({"devis_id": quote["id"], "devis_url": public_url})
        contact.update({"source_devis_id": quote["id"], "dates_formation": session, "lieu": centre_label or city})
        contact.setdefault("formulaire", {})["devis_url"] = public_url
        if created:
            preview = f'<div style="padding:24px"><h2>Devis personnalisé</h2><p><a href="{public_url}">Ouvrir le devis</a></p></div>'
            app_module._crm_activity(contact, "devis", "Devis personnalisé créé", f"Devis n° {quote['id']}", preview)
        return quote

    def project_rows(entry, config):
        rows = [("Formation", _text(config.get("label"))), ("Session et centre", _text(entry.get("formation_date_souhaitee")))]
        cpf = app_module._parse_cpf_value(entry.get("cpf_montant"))
        if cpf:
            rows.append(("Budget CPF déclaré", _euros(cpf)))
        financing = []
        if _yes(entry.get("france_travail")):
            financing.append("étude d’un financement France Travail souhaitée")
        if _yes(entry.get("ft_refus_ok")) or _yes(entry.get("financement_perso")):
            financing.append("financement personnel possible")
        if financing:
            rows.append(("Financement envisagé", " ; ".join(financing)))
        if _yes(entry.get("devis")):
            rows.append(("Devis", "Devis personnalisé généré" if entry.get("devis_url") else "Devis personnalisé demandé"))
        return [(label, value) for label, value in rows if value]

    def session_html(groups):
        if not groups:
            return ""
        centres = []
        for index, centre in enumerate(groups):
            rows = []
            for session in centre.get("sessions", []):
                label = html.escape(_text(session.get("label")))
                badge = html.escape(_text(session.get("badge")))
                badge_html = f' <strong style="color:#3269da;">— {badge}</strong>' if badge else ""
                rows.append(
                    '<tr><td valign="top" width="18" style="width:18px;padding:3px 8px 7px 0;color:#3269da;">•</td>'
                    f'<td valign="top" style="padding:0 0 7px;font-size:14px;line-height:21px;color:#33435c;">{label}{badge_html}</td></tr>'
                )
            separator = ' style="margin-top:18px;padding-top:17px;border-top:1px solid #e1e8f2;"' if index else ""
            centres.append(
                f'<div{separator}><div style="font-size:14px;line-height:21px;font-weight:700;color:#102c5c;">{html.escape(_text(centre.get("centre_label")))}</div>'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-top:7px;">'
                + "".join(rows) + "</table></div>"
            )
        return (
            '<h2 style="margin:30px 0 14px;font-size:20px;line-height:27px;color:#102c5c;">Toutes les prochaines dates de la formation</h2>'
            '<div style="padding:18px 20px;border:1px solid #d9e3f2;background:#f8faff;border-radius:13px;">'
            + "".join(centres) + "</div>"
        )

    def quote_html(public_url):
        if not public_url:
            return ""
        public_url = html.escape(public_url, quote=True)
        return (
            '<div style="margin:28px 0 8px;padding:24px 22px;background:#fff8e5;border:1px solid #f2d88e;border-radius:15px;text-align:center;">'
            '<div style="font-size:18px;line-height:25px;color:#102c5c;font-weight:700;">Votre devis personnalisé est prêt</div>'
            '<div style="margin-top:6px;font-size:14px;line-height:21px;color:#5b6a80;">Il a été généré à partir de la formation, de la session et des solutions de financement évoquées ensemble.</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:18px auto 0;"><tr><td align="center" bgcolor="#f4c45a" style="border-radius:9px;">'
            f'<a href="{public_url}" target="_blank" style="display:inline-block;padding:13px 22px;color:#102c5c;text-decoration:none;font-size:15px;font-weight:700;border-radius:9px;">Consulter mon devis personnalisé</a>'
            '</td></tr></table><div style="margin-top:11px;font-size:12px;line-height:18px;color:#7a6841;">Ce lien est personnel et vous permet de consulter le détail du tarif et du financement.</div></div>'
        )

    def enhance_html(rendered, groups, public_url):
        rendered = rendered.replace("Votre projet&nbsp;: <strong>", "Votre projet&nbsp;: <strong>Formation ", 1)
        sessions = session_html(groups)
        next_steps_marker = '<h2 style="margin:30px 0 14px; font-size:20px; line-height:27px; color:#102c5c;">Vos prochaines étapes</h2>'
        if sessions:
            marker = next_steps_marker if next_steps_marker in rendered else "<!-- Appel à l'action principal -->"
            rendered = rendered.replace(marker, sessions + marker, 1)
        if public_url:
            rendered = rendered.replace("<!-- Appel à l'action principal -->", quote_html(public_url) + "<!-- Appel à l'action principal -->", 1)
        return rendered

    def plain_email(displayed_first_name, config, content, rows, groups, appointment, public_url):
        lines = [
            f"Bonjour {displayed_first_name},", "",
            f"Merci pour le temps accordé lors de notre échange au sujet de la formation {config.get('label')}. Voici la synthèse personnalisée de votre projet et les prochaines étapes utiles.", "",
        ]
        for paragraph in content.get("summary_paragraphs", []):
            if not _text(paragraph).lower().startswith("merci"):
                lines.extend([_text(paragraph), ""])
        lines.append("Votre projet en bref")
        lines.extend(f"- {label} : {value}" for label, value in rows)
        lines.append("")
        if groups:
            lines.append("Toutes les prochaines dates de la formation")
            for centre in groups:
                lines.append(_text(centre.get("centre_label")))
                lines.extend(f"- {_text(session.get('label'))}" for session in centre.get("sessions", []))
            lines.append("")
        if appointment:
            lines.append("Votre rendez-vous")
            if appointment.get("status") == "scheduled":
                detail = "Votre rendez-vous a bien été planifié"
                if appointment.get("date"):
                    detail += f" le {appointment['date']}"
                if appointment.get("time"):
                    detail += f" à {appointment['time']}"
                if appointment.get("mode"):
                    detail += f" — {appointment['mode']}"
                lines.append(detail + ".")
                if appointment.get("url"):
                    lines.append(f"Accéder au rendez-vous : {appointment['url']}")
            else:
                lines.append("Un rendez-vous vous a été proposé. Notre équipe attend votre confirmation du créneau.")
            lines.append("")
        if content.get("next_steps"):
            lines.append("Vos prochaines étapes")
            lines.extend(f"- {_text(step)}" for step in content.get("next_steps", []))
            lines.append("")
        if public_url:
            lines.extend([f"Votre devis personnalisé : {public_url}", ""])
        lines.extend([
            f"Dossier de présentation : {config.get('dossier_url')}", f"Planning : {config.get('planning_url')}", "",
            "Cassandre MENARD", "Responsable commerciale – Intégrale Academy", "04 22 47 07 68",
            "cassandre@integraleacademy.com", "54 chemin du Carreou – 83480 Puget-sur-Argens",
        ])
        return "\n".join(lines)

    def build_email(entry, contact, logo_src=""):
        code = _text(entry.get("formation")).upper()
        config = app_module._secretariat_formation_config(code)
        displayed_first_name = first_name(contact.get("prenom") or _text(entry.get("nom")).split(" ")[0])
        backup = fallback(entry)
        _, centre, city, session = split_session(entry.get("formation_date_souhaitee"))
        facts = {
            "code_formation": code, "intitule_formation": config.get("label"), "session_choisie": session,
            "centre": centre, "ville": city, "objectif": objective(code, config), "tarif_ttc": training_price(code, entry),
            "montant_cpf": app_module._parse_cpf_value(entry.get("cpf_montant")),
            "souhaite_financement_france_travail": _yes(entry.get("france_travail")),
            "statut_france_travail": _text(entry.get("france_travail_status")),
            "financement_personnel_si_refus": _yes(entry.get("ft_refus_ok")),
            "financement_personnel_possible": _yes(entry.get("financement_perso")),
            "identite_numerique_creee": _text(entry.get("identite_numerique")),
            "carte_professionnelle_valide": _text(entry.get("cnaps_ok")) if code in {"APS", "A3P"} else "non_applicable",
            "devis_personnalise_genere": bool(entry.get("devis_url")),
        }
        system = (
            "Tu rédiges la synthèse d'un appel adressée directement au destinataire. Retourne uniquement un objet JSON valide avec "
            "summary_paragraphs (2 à 4 paragraphes) et next_steps (2 à 4 actions). Le mail contient déjà l'introduction « Merci pour le temps accordé lors de notre échange… » : ne la répète jamais. "
            "Commence directement par « Vous souhaitez… » ou « Votre… ». Emploie exclusivement vous, votre et vos. Ne mentionne jamais le prénom, le candidat, le prospect, il ou elle. "
            "Reprends fidèlement la session, le centre, l'objectif, le montant CPF, le tarif et les choix de financement fournis. Un simple souhait de financement France Travail n'est jamais une demande déposée ou en cours. "
            "Pour APS, l'absence de carte professionnelle avant la formation est normale et l'équipe accompagne la démarche d'autorisation CNAPS. N'invente aucune information, aucun statut et aucun montant. Aucun HTML ni Markdown."
        )
        try:
            content = validate_ai(app_module._crm_ai(system, json.dumps(facts, ensure_ascii=False), max_tokens=900), backup, displayed_first_name, entry)
        except Exception as exc:
            print("Compte rendu IA indisponible, fallback personnalisé :", exc)
            content = backup
        rows = project_rows(entry, config)
        appointment = app_module._secretariat_rdv(entry)
        groups = upcoming_sessions(code)
        public_url = _text(entry.get("devis_url"))
        context = {
            "prenom": displayed_first_name, "formation": config, "entry": entry, "content": content,
            "project_rows": rows, "appointment": appointment, "logo_src": logo_src, "ai_url": app_module.SECRETARIAT_AI_URL,
        }
        subject = f"Votre projet {config.get('short') or config.get('label')} – le résumé de notre échange"
        rendered = app_module.render_template("emails/email_resume_echange_integrale.html", **context)
        return subject, plain_email(displayed_first_name, config, content, rows, groups, appointment, public_url), enhance_html(rendered, groups, public_url)

    app_module._crm_format_first_name = first_name
    app_module._secretariat_email_fallback = fallback
    app_module._secretariat_project_rows = project_rows
    app_module._build_secretariat_followup_email = build_email

    def send_with_quote(data, entry, contact):
        quote = ensure_quote(data, entry, contact)
        results = original_send(data, entry, contact)
        if quote and results.get("email") in {"sent", "already_sent"}:
            quote["statut_devis"] = "Envoyé"
            quote["date_envoi_plan"] = quote.get("date_envoi_plan") or datetime.datetime.now(
                pytz.timezone("Europe/Paris")
            ).strftime("%d/%m/%Y %H:%M")
        return results

    app_module._send_secretariat_information_messages = send_with_quote
