"""Informations du catalogue utilisables pendant un appel au secrétariat."""

PUBLIC_CATALOGUE = "https://integraleacademyofficiel-1.onrender.com"
BTS_ADMISSIONS = "https://admissions.integraleconnect.com/"

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
        "prerequisites": "Autorisation préalable du CNAPS ou carte professionnelle valable. Faire vérifier le dossier et la compréhension du français avant l’entrée.",
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
        "prerequisites": "Autorisation préalable du CNAPS ou carte professionnelle en cours de validité. L’équipe accompagne la vérification du dossier.",
        "format": "E-learning et présentiel", "rhythm": "E-learning à terminer avant l’entrée en centre, puis pratique sur site.",
        "location": "Puget-sur-Argens (83)", "capacity": "12 personnes maximum",
        "topics": ["Secourisme, prévention incendie et gestion des conflits", "Cadre légal et consignes de sécurité", "Accueil, filtrage, rondes et interventions", "Alarmes, surveillance électronique et compte rendu"],
        "certification": "TFP Agent de prévention et de sécurité. Après réussite, demande de carte professionnelle auprès du CNAPS.",
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
    "DESP_INIT": {
        "source_url": PUBLIC_CATALOGUE + "/formations-securite/desp-initial",
        "audience": "Créateurs, repreneurs et futurs responsables d’une entreprise de sécurité privée.",
        "prerequisites": "Aucun diplôme ni expérience préalable exigés pour la formation initiale. Le projet est précisé lors de l’entretien.",
        "location": "Puget-sur-Argens, Aurillac ou Paris selon la session",
        "format": "Distanciel et présentiel", "rhythm": "5 semaines à distance (175 h), puis 2 semaines en centre (70 h).",
        "topics": ["Créer ou reprendre une entreprise de sécurité", "Respecter les obligations réglementaires", "Construire un budget et une offre commerciale", "Gérer le personnel et piloter les prestations"],
        "certification": "Titre Dirigeant d’entreprise de sécurité privée, niveau 5. L’agrément dirigeant relève ensuite du CNAPS.",
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
        "prerequisites": "Bac ou titre équivalent, puis étude du dossier. Pour une mission de sécurité privée en entreprise, vérifier la carte professionnelle adaptée avec l’école.",
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
