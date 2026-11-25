"""Informations du catalogue utilisables pendant un appel au secrétariat."""

import calendar
import re
import unicodedata


def is_ssiap3(value):
    """Recognise the level written in contact labels, imports and call transcripts."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return bool(re.search(r"\b(?:ssiap|ssiape|siappe?)\s*(?:3|iii|trois)\b", text)
                or "chef de service de securite incendie" in text)


PUBLIC_CATALOGUE = "https://integraleacademyofficiel-1.onrender.com"
BTS_ADMISSIONS = "https://admissions.integraleconnect.com/"

SSIAP_BOOKING_URL = "https://calendly.com/integraleacademy/ssiap1"
SSIAP3_BOOKING_URL = SSIAP_BOOKING_URL + "?utm_campaign=ssiap3"

SECURITY_AUTHORITY = "Ministère de l'intérieur (CNAPS)"
_MINISTRY_NAME = r"ministère de l['’]intérieur"
_COUNCIL_NAME = r"conseil national des activités privées de sécurité"
_AUTHORITY_MENTION = re.compile(
    rf"(?<![\w/.-])(?:(?:{_MINISTRY_NAME}|{_COUNCIL_NAME})\s*\(CNAPS\)|"
    rf"CNAPS(?:\s*\((?:{_MINISTRY_NAME}|{_COUNCIL_NAME})\))?|{_COUNCIL_NAME})(?![\w/-]|\.\w)", re.I,
)


def authority_wording(text):
    """Use the desk's name for the authority in prose returned by external sources."""
    return _AUTHORITY_MENTION.sub(SECURITY_AUTHORITY, text)

AUTHORIZATION_HELP = (
    "Avez-vous déjà une carte professionnelle ou une autorisation préalable en cours de validité ? "
    "Le centre vérifie l’activité couverte et la date de validité. Si une autorisation est nécessaire, "
    f"le centre de formation prépare et dépose votre demande auprès du {SECURITY_AUTHORITY}, avec votre accord et vos justificatifs. "
    "L’autorisation doit être obtenue avant le premier jour de formation. Pour l’APS, c’est le premier jour de distanciel."
)
FUNDING_REQUEST_HELP = (
    "Le centre de formation s’occupe de votre demande de financement auprès de France Travail. "
    "Pour la démarche que nous proposons, vous devez être inscrit à France Travail et avoir une Identité Numérique La Poste active. "
    "Pendant le rendez-vous téléphonique, notre conseiller prépare la demande de financement avec vous sur Mon Compte Formation. Vous la validez vous-même sur votre téléphone. "
    "France Travail étudie ensuite le dossier et vous communique sa décision."
)
PERMIT_REQUIREMENT = (
    "En règle générale, si vous êtes ressortissant d’un pays hors Union européenne et Espace économique européen, prévoyez un titre valide "
    "autorisant le travail en France et les justificatifs d’au moins cinq ans de détention d’un titre de séjour. "
    "Si votre carte a été renouvelée, les titres successifs peuvent établir cette ancienneté."
)


def residence_permit_help(today):
    year = today.year - 5
    cutoff = today.replace(year=year, day=min(today.day, calendar.monthrange(year, today.month)[1]))
    return (f"{PERMIT_REQUIREMENT} Pour une demande le {today:%d/%m/%Y}, il faut pouvoir justifier "
            f"la détention d’un titre depuis le {cutoff:%d/%m/%Y} au plus tard. "
            "Pour un ressortissant français ou de l’UE/EEE, cette condition de cinq ans ne s’applique pas. "
            "Si vous relevez d’un régime particulier, par exemple comme membre de famille d’un citoyen européen, le conseiller vérifiera les pièces applicables avec vous.")


SST_FEES = {"avec": 1230, "sans": 980}

# Tarifs des sessions par année de début, y compris celles à cheval sur deux ans.
SESSION_FEES = {
    "A3P": {2026: 4200, 2027: 4250},
    "APS": {2026: 1650, 2027: 1700},
    "DESP_INIT": {2026: 4300, 2027: 4350},
    "DESP_VAE": {2026: 3800, 2027: 3850},
}

# Le portail d’admission annonce la campagne 2027. Le jour de convocation
# dépend de la promotion, le mois de rentrée reste un souhait dans le CRM.
BTS_INTAKES = [{
    "start_month": "2027-09", "label": "Septembre 2027",
    "detail": "Promotion 2027–2029 · Jour de rentrée communiqué par l’école.",
}]

PROFESSIONAL_COURSES = {
    "PARCOURS_SECURITE_ALTERNANCE": {
        "assessment": "Évaluations des deux titres professionnels et examen SSIAP 1 à l’issue de leurs modules respectifs.",
        "careers": "Surveillance de sites, protection rapprochée et sécurité incendie, selon les qualifications et autorisations obtenues.",
        "rhythm": "Périodes de cours et missions en entreprise sur un an.",
        "employers": "Entreprises de sécurité privée et services de sécurité recrutant en apprentissage.",
    },
    "A3P": {
        "source_url": PUBLIC_CATALOGUE + "/formations-securite/a3p-apr",
        "audience": "Personnes qui souhaitent exercer la protection rapprochée de particuliers ou de personnalités.",
        "prerequisites": AUTHORIZATION_HELP,
        "duration": "328 h hors examen", "training_hours": "Environ 9 semaines",
        "format": "Présentiel", "rhythm": "41 h de socle commun et 287 h de spécialité.",
        "location": "Puget-sur-Argens (83)",
        "topics": ["Cadre légal, gestion des conflits et secours", "Analyse des menaces et préparation d’une mission", "Reconnaissances, déplacements à pied et en véhicule", "Protection en équipe et mise à l’abri"],
        "certification": "TFP Agent de protection physique des personnes, niveau 4.",
        "assessment": "Questionnaires de connaissances et mise en situation de protection rapprochée devant un jury.",
        "careers": "Agent de protection rapprochée, garde du corps ou membre d’une équipe de protection.",
        "extra_label": "Hébergement", "extra": "Dortoir collectif sur réservation. Pour un départ en 2027, 350 € TTC pour la durée du parcours.",
    },
    "APS": {
        "source_url": PUBLIC_CATALOGUE + "/formations-securite/aps",
        "audience": "Candidats à un premier poste de surveillance humaine ou de gardiennage.",
        "prerequisites": AUTHORIZATION_HELP,
        "format": "E-learning et présentiel", "rhythm": "175 heures au total. Vous commencez par 51 heures en distanciel, puis vous suivez 124 heures en présentiel à Puget-sur-Argens.",
        "location": "Puget-sur-Argens (83)", "capacity": "12 personnes maximum",
        "topics": ["Secourisme, prévention incendie et gestion des conflits", "Cadre légal et consignes de sécurité", "Accueil, filtrage, rondes et interventions", "Alarmes, surveillance électronique et compte rendu"],
        "certification": f"TFP Agent de prévention et de sécurité. Après réussite, la carte professionnelle se demande auprès du {SECURITY_AUTHORITY} avant de pouvoir exercer.",
        "assessment": "Questions de connaissances et mises en situation individuelles. L’examen se déroule en présentiel.",
        "careers": "Agent de sécurité en entreprise, en magasin, sur un site industriel ou lors d’événements.",
    },
    "SSIAP": {
        "source_url": PUBLIC_CATALOGUE + "/formations-securite/ssiap-1",
        "audience": "Candidats à un poste de sécurité incendie dans un établissement recevant du public ou un immeuble de grande hauteur.",
        "prerequisites": "Secourisme en cours de validité, certificat médical de moins de trois mois et aptitude à renseigner une main courante.",
        "location": "Puget-sur-Argens (83)", "rhythm": "Cours, exercices pratiques et rondes sur installations techniques.",
        "topics": ["Feu, propagation et moyens d’extinction", "Prévention et installations de sécurité incendie", "Rondes, poste de sécurité, alerte et évacuation", "Assistance aux personnes et accueil des secours"],
        "certification": "Diplôme SSIAP 1, agent des services de sécurité incendie et d’assistance à personnes.",
        "assessment": "QCM et ronde pratique avec anomalies et intervention sur un sinistre.",
        "careers": "Agent de sécurité incendie dans un centre commercial, un hôpital, un site événementiel ou un immeuble de grande hauteur.",
        "funding": "Employeur, France Travail ou financement personnel. CPF pour les offres éligibles selon le dossier.",
        "extra_label": "Choix de la formule", "extra": "SSIAP 1 seul si le secourisme est valable. Choisir une session avec SST si le candidat doit aussi préparer le secourisme.",
    },
    "SSIAP3": {
        "source_url": PUBLIC_CATALOGUE + "/formations-securite/ssiap-3",
        "dossier_url": PUBLIC_CATALOGUE + "/documents/programme-ssiap-3.pdf",
        "audience": "Professionnels souhaitant diriger un service de sécurité incendie en ERP ou en IGH.",
        "prerequisites": "Diplôme de niveau 4 minimum, ou SSIAP 2 (ou ancien ERP 2 / IGH 2) avec trois ans d’exercice comme chef d’équipe. PSC de moins de deux ans, SST ou PSE 1 en cours de validité.",
        "location": "Puget-sur-Argens (83)", "capacity": "10 stagiaires maximum",
        "rhythm": "Cours en présentiel, études de cas et préparation aux épreuves.",
        "topics": ["Réglementation incendie et étude de plans", "Gestion des risques et commissions de sécurité", "Conseil au chef d’établissement", "Management et budget du service"],
        "certification": "Diplôme SSIAP 3 de chef de service de sécurité incendie et d’assistance à personnes.",
        "assessment": "QCM, rédaction d’une notice technique de sécurité et entretien devant un jury.",
        "careers": "Chef de service de sécurité incendie en établissement recevant du public ou en immeuble de grande hauteur.",
    },
    "DESP_INIT": {
        "source_url": PUBLIC_CATALOGUE + "/formations-securite/desp-initial",
        "audience": "Créateurs, repreneurs et futurs responsables d’une entreprise de sécurité privée.",
        "prerequisites": "Être majeur et maîtriser le français. Niveau bac ou équivalent, SSIAP 3, expérience à responsabilités ou avis favorable à l’entretien d’admission.",
        "location": "Puget-sur-Argens ou Paris selon la session",
        "format": "Distanciel et présentiel", "rhythm": "5 semaines à distance (175 h), puis 2 semaines en centre (70 h).",
        "topics": ["Créer ou reprendre une entreprise de sécurité", "Respecter les obligations réglementaires", "Construire un budget et une offre commerciale", "Gérer le personnel et piloter les prestations"],
        "certification": f"Titre Dirigeant d’entreprise de sécurité privée, niveau 5. L’agrément dirigeant relève ensuite du {SECURITY_AUTHORITY}.",
        "assessment": "Évaluations de connaissances, mises en situation de gestion et passage devant le jury.",
        "careers": "Dirigeant d’entreprise de sécurité, directeur d’agence ou responsable d’exploitation.",
    },
    "DESP_VAE": {
        "source_url": PUBLIC_CATALOGUE + "/formations-securite/desp-vae",
        "audience": "Professionnels ayant déjà exercé des responsabilités de gestion, de management ou de direction en sécurité privée.",
        "prerequisites": "Expérience directement liée aux activités du titre. Le dossier de faisabilité permet d’étudier la recevabilité.",
        "location": "Accompagnement à distance", "duration": "Calendrier individuel",
        "format": "Accompagnement à distance", "rhythm": "Entretiens et rédaction du dossier à son rythme, puis convocation au jury.",
        "topics": ["Analyser les activités déjà exercées", "Préparer le dossier de faisabilité", "Rédiger le dossier de validation et réunir les preuves", "Présenter son expérience au jury"],
        "certification": "Même titre DESP de niveau 5 que par la voie initiale, après décision du jury.",
        "assessment": "Examen du dossier de validation et entretien avec le jury selon la convocation du certificateur.",
        "careers": "Faire reconnaître son expérience pour diriger une structure de sécurité ou évoluer vers la direction d’agence.",
        "start_summary": "Calendrier individuel après étude de faisabilité.",
        "flexible_start": "Le démarrage de l’accompagnement se définit après l’étude de faisabilité. Le jury fait l’objet d’une convocation distincte.",
    },
    "VTC": {
        "label": "Chauffeur VTC",
        "source_url": PUBLIC_CATALOGUE + "/vtc",
        "audience": "Candidats au métier de conducteur de voiture de transport avec chauffeur.",
        "prerequisites": "Permis B depuis 3 ans (2 ans en conduite accompagnée). Vérifier l’aptitude médicale et les conditions de délivrance de la carte VTC.",
        "duration": "105 h estimées", "format": "E-learning et pratique",
        "rhythm": "Théorie en ligne à son rythme, puis préparation pratique sur rendez-vous.",
        "location": "Théorie à distance · pratique en Côte d’Azur",
        "topics": ["Réglementation du transport, sécurité routière et gestion", "Français, anglais et relation client", "Préparer un itinéraire et une prestation commerciale", "Conduite en véhicule à double commande"],
        "certification": "Préparation à l’examen VTC de la Chambre de métiers et de l’artisanat. La carte professionnelle se demande après réussite.",
        "assessment": "Épreuves théoriques d’admissibilité puis épreuve pratique de conduite et de prise en charge d’un client.",
        "careers": "Chauffeur VTC salarié ou exploitant indépendant.",
        "start_summary": "Théorie accessible dès la finalisation de l’inscription.",
        "flexible_start": "La théorie démarre après la finalisation de l’inscription. La CMA fixe séparément les dates d’examen.",
        "extra_label": "Examen pratique uniquement",
        "extra": "Pour les candidats déjà admissibles. Prêt du véhicule à 150 €, ou préparation pratique avec prêt à 600 €. Examen à Saint-Laurent-du-Var (06) ou La Valette-du-Var (83).",
        "action_url": "https://admissions.integraleconnect.com/vtc", "action_label": "Formules pratiques VTC",
    },
}

BTS_COURSES = {
    "BTS_MOS": {
        "path": "mos", "audience": "Candidats qui souhaitent préparer et encadrer des prestations de sécurité.",
        "prerequisites": f"Bac ou titre équivalent, puis étude du dossier. Pour ce parcours en sécurité privée, détenir la carte professionnelle du {SECURITY_AUTHORITY} avant l’entrée en formation.",
        "rhythm": "15 jours à l’école, puis 15 jours en entreprise.", "training_hours": "1 350 h de formation",
        "topics": ["Analyser les risques et préparer un dispositif de sécurité", "Organiser les équipes et leurs plannings", "Piloter la prestation, la qualité et la relation client"],
        "assessment": "Épreuves du BTS sur la préparation d’une prestation, le management des équipes et la relation client, en plus des matières générales.",
        "employers": "Entreprises de sécurité privée, services de sécurité internes et prestataires événementiels.",
        "careers": "Chef de site, chef d’équipe ou assistant d’exploitation en sécurité.",
        "rncp": "41000",
    },
    "BTS_MCO": {
        "path": "mco", "audience": "Candidats attirés par la vente, la gestion d’un point de vente et l’animation d’équipe.",
        "topics": ["Conseiller et fidéliser les clients", "Mettre en valeur l’offre et animer le point de vente", "Suivre les stocks, les marges et les résultats", "Répartir les missions de l’équipe commerciale"],
        "assessment": "Situations professionnelles de vente et d’animation commerciale, gestion opérationnelle et management d’équipe.",
        "employers": "Magasins spécialisés, grandes enseignes, réseaux de distribution ou agences de services.",
        "careers": "Conseiller de vente, manager adjoint ou responsable de rayon.",
        "rncp": "38362",
    },
    "BTS_NDRC": {
        "path": "ndrc", "audience": "Candidats intéressés par la prospection, la négociation et les outils de relation client en ligne.",
        "topics": ["Prospecter et négocier une offre", "Suivre un portefeuille et fidéliser à distance", "Animer les canaux de vente numériques", "Développer un réseau de distributeurs ou de partenaires"],
        "assessment": "Épreuves de négociation-vente, de relation client à distance et de digitalisation, et présentation d’activités d’animation de réseaux.",
        "employers": "Services commerciaux, agences, distributeurs, centres de relation client ou réseaux de vente.",
        "careers": "Commercial terrain ou sédentaire, chargé de clientèle ou animateur de réseau.",
        "rncp": "38368",
    },
    "BTS_CI": {
        "path": "commerce-international", "audience": "Candidats qui souhaitent travailler en langues étrangères et organiser des opérations internationales.",
        "topics": ["Communiquer en contexte interculturel", "Coordonner transport, documents et opérations d’import-export", "Identifier des marchés et des partenaires à l’étranger", "Préparer et suivre la prospection internationale"],
        "assessment": "Épreuves de relation commerciale interculturelle, d’opérations internationales, de développement commercial et de langues vivantes.",
        "employers": "Services import-export, transitaires, transporteurs ou entreprises qui commercialisent à l’étranger.",
        "careers": "Assistant import-export, gestionnaire ADV export ou agent de transit.",
        "rncp": "41759",
    },
    "BTS_PI": {
        "path": "professions-immobilieres", "audience": "Candidats qui souhaitent accompagner les clients dans leurs projets de vente, de location ou de copropriété.",
        "topics": ["Prospecter, évaluer un bien et organiser les visites", "Préparer les baux et assurer la gestion locative", "Participer aux assemblées et au suivi des copropriétés", "Conseiller sur les travaux et la rénovation"],
        "assessment": "Épreuves professionnelles sur les projets immobiliers, la gestion des copropriétés et le conseil au client.",
        "employers": "Agences immobilières, cabinets d’administration de biens, syndics et bailleurs sociaux.",
        "careers": "Conseiller immobilier, gestionnaire locatif ou assistant de copropriété.",
    },
    "BTS_CG": {
        "path": "comptabilite-gestion", "audience": "Candidats intéressés par les chiffres, l’organisation comptable et l’analyse financière.",
        "topics": ["Contrôler et enregistrer les opérations comptables", "Préparer les déclarations fiscales et les éléments de paie", "Analyser les coûts, la trésorerie et la performance", "Fiabiliser les données avec les logiciels de gestion"],
        "assessment": "Épreuves de traitement comptable, fiscal et social, analyse de gestion et présentation de situations professionnelles.",
        "employers": "Cabinets d’expertise comptable, services comptables d’entreprises, PME ou associations.",
        "careers": "Assistant comptable, collaborateur de cabinet ou comptable clients et fournisseurs.",
        "rncp": "39159",
    },
}


# Questions que le secrétariat peut lire au téléphone, dans l’ordre de l’entretien.
PRIVATE_SECURITY_CHECKS = [
    ("Autorisation d’entrée en formation", AUTHORIZATION_HELP),
    ("Français", "Avant l’entrée, vous passez un test de compréhension des consignes, de lecture et de rédaction d’un compte rendu simple. Si vous êtes de nationalité étrangère, y compris européenne, préparez aussi un justificatif reconnu de français de niveau B1 minimum, par exemple un DELF B1."),
    ("Enquête administrative", f"Le {SECURITY_AUTHORITY} examine la compatibilité des antécédents avec le métier. Il peut notamment consulter le bulletin n° 2 et le traitement des antécédents judiciaires (TAJ). Si vous avez un doute, le conseiller reprend la situation avec vous."),
    ("Séjour et pièces étrangères", PERMIT_REQUIREMENT + " Selon votre situation, préparez aussi un extrait de casier judiciaire du pays d’origine ou de provenance datant de moins de trois mois, avec sa traduction en français."),
    ("Pièce d’identité", "Préparez un document en cours de validité. Pour une carte d’identité ou un titre de séjour, il faut une copie lisible du recto et du verso. Pour un passeport, il faut la page avec votre photo et votre identité."),
    ("Justificatif de domicile", "Préparez un justificatif de moins de trois mois, par exemple une facture d’électricité ou d’eau. Si vous êtes hébergé, il faut l’attestation signée de l’hébergeant, sa pièce d’identité et son justificatif de domicile de moins de trois mois."),
]
BTS_CHECKS = [
    ("Diplôme", "Quel baccalauréat préparez-vous ou avez-vous obtenu ? Un titre de niveau équivalent permet aussi de candidater."),
    ("Admission", "Le service BTS étudie le dossier et le projet professionnel. La candidature se dépose sur le portail des admissions de l’école."),
    ("Entreprise", "Avez-vous déjà un employeur pour l’alternance ? Le contrat d’apprentissage permet de suivre les cours tout en exerçant en entreprise."),
]
FIRST_AID_CHECK = ("Secourisme", "Quel diplôme de premiers secours avez-vous, et de quand date-t-il ? Il faut un PSC ou PSC1 de moins de deux ans, ou un certificat SST ou PSE 1 en cours de validité.")

DESK_ADMISSION = {
    "A3P": [*PRIVATE_SECURITY_CHECKS, ("Activité et pratique", "L’autorisation demandée doit couvrir la protection physique des personnes. Pouvez-vous participer aux exercices pratiques ? Signalez au conseiller tout besoin d’aménagement avant l’entrée.")],
    "APS": PRIVATE_SECURITY_CHECKS,
    "SSIAP": [FIRST_AID_CHECK, ("Aptitude physique", "Un médecin doit remplir le certificat médical réglementaire moins de trois mois avant l’entrée en formation."),
              ("Expression écrite", "Le candidat doit comprendre les consignes et pouvoir décrire par écrit un incident dans le registre du poste de sécurité, appelé main courante.")],
    "SSIAP3": [("Voie diplôme", "Êtes-vous titulaire d’un diplôme de niveau 4, soit le niveau du baccalauréat, ou d’un niveau supérieur ?"),
               ("Autre voie d’accès", "À défaut, il faut un SSIAP 2 ou l’ancien diplôme ERP 2 ou IGH 2, avec trois ans d’exercice comme chef d’équipe. Préparez le diplôme et les justificatifs d’emploi."), FIRST_AID_CHECK],
    "DESP_INIT": [("Projet", "Souhaitez-vous créer, reprendre ou diriger une entreprise de sécurité privée ? Il faut être majeur et maîtriser le français oral et écrit."),
                  ("Admission", "Demandez si la personne a le niveau bac, le SSIAP 3 ou une expérience à responsabilités. L’entretien d’admission permet aussi d’étudier un autre profil."),
                  ("Après le titre", f"Pour diriger l’entreprise, une demande distincte d’agrément est à déposer auprès du {SECURITY_AUTHORITY}.")],
    "DESP_VAE": [("Expérience", "Quelles responsabilités avez-vous déjà exercées en sécurité privée ? Par exemple, gérer une équipe, un budget, des contrats ou l’exploitation d’une agence."),
                 ("Justificatifs", "Prévoyez des preuves de ces activités, comme une fiche de poste, une attestation d’employeur ou des réalisations professionnelles."),
                 ("Recevabilité", "Le certificateur examine d’abord le dossier de faisabilité. Si la demande est recevable, le candidat prépare son dossier de validation puis le présente au jury.")],
    "VTC": [("Permis", "Depuis quand avez-vous le permis B ? Il faut trois ans de permis, ou deux ans après la conduite accompagnée."),
            ("Aptitude médicale", "Pour exercer, il faut un avis favorable d’un médecin agréé par la préfecture pour la conduite professionnelle."),
            ("Accès au métier", "L’examen comprend la théorie puis la pratique. Après la réussite, le candidat demande sa carte professionnelle de chauffeur VTC à la préfecture.")],
    "PARCOURS_SECURITE_ALTERNANCE": [("Profil", "Ce parcours de l’école s’adresse aux moins de 26 ans. Avez-vous le permis B et pouvez-vous vous déplacer entre le centre et l’entreprise ?"),
                                     ("Apprentissage", "Vérifiez le droit de travailler en France et la disponibilité pendant un an. Le parcours alterne cours au centre et périodes chez l’employeur."),
                                     ("Accès aux modules", f"Pour la sécurité privée, il faut le titre d’accès du {SECURITY_AUTHORITY}. Le module incendie comporte aussi des conditions de secourisme et d’aptitude médicale.")],
    "BTS_MOS": [*BTS_CHECKS, ("Sécurité privée", f"Pour le parcours MOS de l’école en sécurité privée, la carte professionnelle du {SECURITY_AUTHORITY} doit être valide à l’entrée. Si vous débutez, demandez également des renseignements sur la formation APS.")],
}

DESK_INTRODUCTIONS = {
    "A3P": "Vous voulez travailler comme garde du corps ? Cette formation vous prépare à protéger un client au quotidien, à organiser ses déplacements et à réagir si sa sécurité est menacée. Le métier s’appelle aussi agent de protection rapprochée (APR).",
    "APS": "Vous pouvez débuter en sécurité privée sans expérience dans le métier. Cette formation prépare aux missions d’un agent de sécurité. Sur un site, vous contrôlez les accès, effectuez des rondes et intervenez en cas d’incident. Après réussite à l’examen, vous pouvez demander la carte professionnelle nécessaire pour exercer.",
    "SSIAP": "Le SSIAP 1 prépare au métier d’agent de sécurité incendie. Dans un hôpital, un centre commercial ou un immeuble de grande hauteur, vous surveillez les installations, donnez l’alerte et aidez le public à évacuer.",
    "SSIAP3": "Le SSIAP 3 prépare à diriger un service de sécurité incendie. Vous encadrez les équipes, conseillez la direction du bâtiment et préparez les visites de la commission de sécurité.",
    "DESP_INIT": "Ce parcours prépare à créer, reprendre ou diriger une entreprise de sécurité privée. Vous apprenez à chiffrer une prestation, organiser les équipes et respecter les obligations du dirigeant.",
    "DESP_VAE": "Si vous avez déjà exercé des responsabilités en sécurité privée, la validation des acquis de l’expérience (VAE) vous permet de présenter ce que vous savez faire à un jury pour obtenir le titre de dirigeant.",
    "VTC": "Vous souhaitez transporter des clients sur réservation ? Le parcours prépare aux deux épreuves de l’examen de chauffeur VTC, avec la théorie en ligne et la préparation à la conduite avec un formateur.",
    "PARCOURS_SECURITE_ALTERNANCE": "En un an, vous préparez trois qualifications pour travailler dans la surveillance, la protection rapprochée et la sécurité incendie. Vous alternez les cours à l’école et les missions en entreprise.",
    "BTS_MOS": "Ce BTS prépare à organiser la sécurité d’un site ou d’un événement. Vous apprenez à dimensionner une équipe, préparer son planning et suivre la prestation avec le client.",
    "BTS_MCO": "Ce BTS prépare à gérer un rayon ou un point de vente. Vous apprenez à conseiller les clients, suivre les ventes et les stocks, et animer une équipe.",
    "BTS_NDRC": "Ce BTS forme aux métiers commerciaux. Vous apprenez à trouver des clients, négocier une vente et fidéliser votre clientèle en rendez-vous, par téléphone et en ligne.",
    "BTS_CI": "Ce BTS prépare à travailler avec des clients et fournisseurs à l’étranger. Vous apprenez à suivre les ventes, les achats et les expéditions à l’international.",
    "BTS_PI": "Ce BTS prépare aux métiers de l’immobilier en agence ou chez un syndic. Vous apprenez à accompagner une vente, mettre un bien en location et participer à la gestion d’une copropriété.",
    "BTS_CG": "Ce BTS prépare à travailler dans un service comptable ou en cabinet. Vous apprenez à enregistrer les opérations, contrôler les factures et préparer les déclarations de l’entreprise.",
}
