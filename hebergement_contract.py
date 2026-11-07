"""Génération de la convention d'hébergement PDF d'Intégrale Academy."""

from __future__ import annotations

import base64
import html
import io
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


NAVY = colors.HexColor("#182132")
NAVY_2 = colors.HexColor("#27364D")
GOLD = colors.HexColor("#B78616")
GOLD_LIGHT = colors.HexColor("#F7F0DE")
GOLD_PALE = colors.HexColor("#FCF8EE")
INK = colors.HexColor("#273142")
MUTED = colors.HexColor("#667085")
LINE = colors.HexColor("#D7DDE6")
LIGHT = colors.HexColor("#F4F6F9")
RED = colors.HexColor("#A3312A")
RED_LIGHT = colors.HexColor("#FCEDEB")
WHITE = colors.white


ARRIVAL_LATE_COPY = (
    "Si l'Occupant ne peut pas se présenter avant 17h00, il doit organiser et "
    "financer par ses propres moyens une solution d'hébergement pour cette nuit, "
    "puis se présenter au Centre le lendemain matin à l'heure indiquée sur sa "
    "convocation. Le Centre ne prend pas en charge le coût de cette nuit extérieure."
)
PARTICIPATION_COPY = (
    "La participation forfaitaire de 300 € doit être versée dès l'arrivée, au "
    "moment de la signature de la convention et de la remise des clés. Aucun accès "
    "à l'hébergement ne peut être accordé tant que cette somme n'a pas été remise."
)
DEPOSIT_COPY = (
    "Un chèque de caution distinct de 200 € doit être remis dès l'arrivée, au "
    "moment de la signature de la convention et de la remise des clés. Il ne peut "
    "pas être remplacé par le règlement de la participation de 300 €."
)
FINAL_ACKNOWLEDGEMENT_COPY = (
    "J'ai pris connaissance des sommes à verser, du matériel à apporter et de "
    "l'ensemble des règles de l'hébergement. Je reconnais avoir pu poser mes "
    "questions avant de signer."
)


def _safe(value: object, fallback: str = "") -> str:
    text = str(value or fallback)
    return html.escape(text, quote=True).replace("\n", "<br/>")


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ContractBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=11,
        leading=16,
        textColor=INK,
        alignment=TA_LEFT,
        spaceAfter=9,
    ))
    styles.add(ParagraphStyle(
        name="ContractSmall",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=MUTED,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="ContractTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=24,
        leading=28,
        textColor=NAVY,
        alignment=TA_LEFT,
        spaceAfter=9,
    ))
    styles.add(ParagraphStyle(
        name="ContractSubtitle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=13,
        textColor=MUTED,
        spaceAfter=14,
    ))
    styles.add(ParagraphStyle(
        name="ContractH2",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=19,
        textColor=NAVY,
        spaceBefore=14,
        spaceAfter=8,
        keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="ContractH3",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=NAVY,
        spaceBefore=6,
        spaceAfter=6,
        keepWithNext=True,
    ))
    styles.add(ParagraphStyle(
        name="ContractBullet",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10.75,
        leading=15.5,
        textColor=INK,
        leftIndent=10,
        firstLineIndent=-8,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="ContractKicker",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8.2,
        leading=10,
        textColor=GOLD,
        spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="Cell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=INK,
        spaceAfter=0,
    ))
    styles.add(ParagraphStyle(
        name="CellLabel",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=14,
        textColor=NAVY,
        spaceAfter=0,
    ))
    styles.add(ParagraphStyle(
        name="CellHeader",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=WHITE,
        alignment=TA_CENTER,
        spaceAfter=0,
    ))
    styles.add(ParagraphStyle(
        name="Signature",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=INK,
        spaceAfter=0,
    ))
    return styles


def _body(text: str, styles) -> Paragraph:
    return Paragraph(text, styles["ContractBody"])


def _heading(text: str, styles) -> Paragraph:
    return Paragraph(text, styles["ContractH2"])


def _subheading(text: str, styles) -> Paragraph:
    return Paragraph(text, styles["ContractH3"])


def _bullets(story, items, styles):
    for item in items:
        story.append(Paragraph(f"&#8226;&nbsp; {_safe(item)}", styles["ContractBullet"]))


def _key_value_table(rows, styles, label_width=43 * mm):
    data = []
    for label, value in rows:
        data.append([
            Paragraph(_safe(label), styles["CellLabel"]),
            Paragraph(value, styles["Cell"]),
        ])
    table = Table(data, colWidths=[label_width, 168 * mm - label_width], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def _callout(title, text, styles, danger=False):
    title_color = RED if danger else NAVY
    border_color = RED if danger else GOLD
    background = RED_LIGHT if danger else GOLD_PALE
    content = Paragraph(
        f'<font color="{title_color.hexval()}"><b>{_safe(title)}</b></font><br/>{_safe(text)}',
        styles["ContractBody"],
    )
    table = Table([[content]], colWidths=[168 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("BOX", (0, 0), (-1, -1), 0.8, border_color),
        ("LINEBEFORE", (0, 0), (0, -1), 3, border_color),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return KeepTogether([Spacer(1, 5), table, Spacer(1, 7)])


def _signature_table(occupant_name, occupant_note, centre_visa, styles):
    left = [
        Paragraph("<b>POUR LE CENTRE</b>", styles["Signature"]),
        Spacer(1, 4),
        Paragraph("Clément VAILLANT<br/>Directeur général", styles["Signature"]),
        Spacer(1, 8),
    ]
    if centre_visa:
        mark = Image(io.BytesIO(centre_visa), width=68 * mm, height=17 * mm)
        mark.hAlign = "LEFT"
        left.append(mark)
    else:
        left.append(Paragraph("Signature et cachet du centre :", styles["Signature"]))
    right = [
        Paragraph(f"<b>L'OCCUPANT</b><br/>{_safe(occupant_name)}", styles["Signature"]),
        Spacer(1, 6),
        Paragraph(_safe(occupant_note), styles["Signature"]),
        Spacer(1, 5),
        Paragraph("Signature :", styles["Signature"]),
    ]
    table = Table([[left, right]], colWidths=[84 * mm, 84 * mm],
                  minRowHeights=[57 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.55, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def _chapter(title, styles):
    return [
        PageBreak(),
        Paragraph("CONVENTION D’HÉBERGEMENT TEMPORAIRE", styles["ContractKicker"]),
        Paragraph(_safe(title), styles["ContractTitle"]),
        Spacer(1, 4 * mm),
    ]


class _ContractDocument(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "ContractTitle":
            anchor = f"rubrique-{self.page}"
            self.canv.bookmarkPage(anchor)
            self.canv.addOutlineEntry(flowable.getPlainText(), anchor)


def _acknowledgement_table(items, styles):
    ack_data = [[
        Paragraph("Visa", styles["CellHeader"]),
        Paragraph("Engagement", styles["CellHeader"]),
        Paragraph("Déclaration de l'Occupant", styles["CellHeader"]),
    ]]
    for label, declaration in items:
        ack_data.append([
            Paragraph("[&nbsp;&nbsp;]", styles["Cell"]),
            Paragraph(f"<b>{_safe(label)}</b>", styles["Cell"]),
            Paragraph(_safe(declaration), styles["Cell"]),
        ])
    ack_table = Table(ack_data, colWidths=[15 * mm, 39 * mm, 114 * mm], repeatRows=1, hAlign="LEFT")
    ack_style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]
    for row_index in range(2, len(ack_data), 2):
        ack_style.append(("BACKGROUND", (0, row_index), (-1, row_index), LIGHT))
    ack_table.setStyle(TableStyle(ack_style))
    return ack_table


def _annex_header(number, title, subtitle, styles):
    return [
        Paragraph(f"ANNEXE {number} - DOCUMENT CONTRACTUEL", styles["ContractKicker"]),
        Paragraph(_safe(title), styles["ContractTitle"]),
        Paragraph(_safe(subtitle), styles["ContractSubtitle"]),
    ]


def _draw_header_footer(canvas, doc, contract_reference):
    canvas.saveState()
    page_width, page_height = A4
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.45)
    canvas.line(17 * mm, page_height - 14 * mm, page_width - 17 * mm, page_height - 14 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica-Bold", 6.9)
    canvas.drawRightString(
        page_width - 17 * mm,
        page_height - 11 * mm,
        "INTEGRALE ACADEMY  |  HEBERGEMENT STAGIAIRES",
    )
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(17 * mm, 9 * mm, "Paraphe de l'occupant : __________")
    canvas.drawCentredString(
        page_width / 2,
        9 * mm,
        f"Convention d'hebergement - Ref. {contract_reference}",
    )
    canvas.drawRightString(
        page_width - 17 * mm,
        9 * mm,
        f"Page {canvas.getPageNumber()}",
    )
    canvas.restoreState()


def build_hebergement_contract_pdf(context: dict, logo_path: str | None = None) -> bytes:
    """Construit une convention préremplie et retourne ses octets PDF."""
    styles = _styles()
    output = io.BytesIO()
    reference = str(context.get("contract_reference") or "HEB-CDA")
    doc = _ContractDocument(
        output,
        pagesize=A4,
        rightMargin=21 * mm,
        leftMargin=21 * mm,
        topMargin=19 * mm,
        bottomMargin=17 * mm,
        title="Convention d'hébergement temporaire - Intégrale Academy",
        author="Intégrale Academy",
        subject="Hébergement collectif accessoire à une action de formation",
        pageCompression=1,
    )
    story = []
    occupant = context.get("occupant") or {}
    occupant_name = " ".join(filter(None, [
        str(occupant.get("nom") or "").strip(),
        str(occupant.get("prenom") or "").strip(),
    ])) or "À compléter"
    formation = str(context.get("formation_label") or "À compléter")
    session = str(context.get("session_label") or "À compléter")
    arrival = str(context.get("arrival_label") or "la veille du premier jour de formation")
    arrival_time = str(context.get("arrival_time") or "____ h ____")
    start = str(context.get("formation_start_label") or "À compléter")
    end = str(context.get("formation_end_label") or "À compléter")
    departure = str(context.get("departure_label") or end)
    departure_time = str(context.get("departure_time") or "____ h ____")
    contract_date = str(context.get("contract_date_label") or "À compléter")
    contract_time = str(context.get("contract_time") or "____ h ____")
    key_number = _safe(context.get("key_number"), "________________")
    reception_agent = str(context.get("reception_agent") or "").strip()
    if reception_agent.casefold() in {"clément", "clement", "clément vaillant", "clement vaillant"}:
        reception_agent = "Clément VAILLANT"
    configured_visa = os.environ.get("HEBERGEMENT_CENTRE_VISA_PNG_BASE64", "")
    centre_visa = base64.b64decode(configured_visa, validate=True) if configured_visa else None

    brand_cells = []
    if logo_path and os.path.exists(logo_path):
        reader = ImageReader(logo_path)
        width, height = reader.getSize()
        logo_width = 23 * mm
        brand_cells.append(Image(logo_path, width=logo_width, height=logo_width * height / width))
    else:
        brand_cells.append(Paragraph("<b>IA</b>", styles["ContractTitle"]))
    legal = Paragraph(
        "<para align=right><b>INTÉGRALE ACADEMY</b><br/>"
        '<font color="#667085" size="7.2">SASU Intégrale Sécurité Formations<br/>'
        "54 chemin du Carreou - 83480 Puget-sur-Argens<br/>"
        "SIREN 840 899 884 - RCS Fréjus - NDA 93830600283<br/>Accueil : 04 22 47 07 68</font></para>",
        styles["ContractBody"],
    )
    brand_cells.append(legal)
    brand = Table([brand_cells], colWidths=[30 * mm, 138 * mm])
    brand.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.extend([
        brand,
        Spacer(1, 9 * mm),
        Paragraph("HÉBERGEMENT COLLECTIF SUR LE SITE DE PUGET-SUR-ARGENS", styles["ContractKicker"]),
        Paragraph("CONVENTION D'HÉBERGEMENT TEMPORAIRE", styles["ContractTitle"]),
        Paragraph(
            "Accessoire à une action de formation - À signer avant la remise des clés",
            styles["ContractSubtitle"],
        ),
    ])

    metric_data = [
        ("ARRIVÉE", f"{_safe(arrival)}<br/>08h00 - 17h00"),
        ("PARTICIPATION", "300 €<br/>à verser dès l'arrivée"),
        ("CAUTION", "Chèque de 200 €<br/>à remettre dès l'arrivée"),
        ("CALME", "Aucun bruit<br/>après 22h00"),
    ]
    metrics = Table([[Paragraph(
        f'<font color="#B78616" size="8"><b>{label}</b></font><br/>'
        f'<font color="#182132" size="9.5"><b>{value}</b></font>',
        styles["Cell"],
    ) for label, value in metric_data]], colWidths=[42 * mm] * 4, minRowHeights=[24 * mm], hAlign="LEFT")
    metrics.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GOLD_LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E3D3A7")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.extend([
        metrics,
        _heading("Votre séjour", styles),
        _key_value_table([
            ("Occupant", _safe(occupant_name)),
            ("Référence", f"{_safe(reference)} - Version du {_safe(context.get('contract_version'))}<br/><font color=\"#667085\" size=\"7\">Document généré le {_safe(context.get('generated_on'))}</font>"),
            ("Formation", _safe(formation)),
            ("Session", _safe(session)),
            ("Période de formation", f"Du {_safe(start)} au {_safe(end)}"),
            ("Période d’hébergement", f"Arrivée : {_safe(arrival)} à {_safe(arrival_time)}<br/>Départ prévu : {_safe(departure)} à {_safe(departure_time)}"),
            ("Clé d’entrée n°", key_number),
        ], styles),
        _callout(
            "Condition préalable à l'accès",
            "La présente convention doit être lue et signée au premier jour "
            "d'occupation, avant toute remise de clé. La participation de 300 € et "
            "le chèque de caution de 200 € sont remis impérativement au même moment.",
            styles,
        ),
        _subheading("Où signer les deux exemplaires ?", styles),
        _body("Trois cadres sont prévus pour votre signature : l’article 19, la fiche de remise "
              "de la clé et des sommes (annexe 1), puis l’attestation des règles (annexe 2).", styles),
        *_chapter("Les parties et leur engagement", styles),
        _heading("Entre les soussignés", styles),
        _body(
            "<b>D'une part</b>, la SASU Intégrale Sécurité Formations, exerçant sous "
            "l'enseigne Intégrale Academy, dont le siège social est situé 54 chemin "
            "du Carreou, 83480 Puget-sur-Argens, immatriculée au RCS de Fréjus sous le "
            "numéro 840 899 884, représentée par Clément VAILLANT, "
            "directeur général, ci-après désignée « le Centre ».",
            styles,
        ),
        _body(
            "<b>Et d'autre part</b>, le stagiaire bénéficiaire de l'hébergement, "
            "ci-après désigné « l'Occupant » :",
            styles,
        ),
        _key_value_table([
            ("Nom et prénom", _safe(occupant_name)),
            ("Adresse personnelle", f"{_safe(occupant.get('address'), '____________________________________________')}<br/>{_safe(occupant.get('postal_code'), '________')} {_safe(occupant.get('city'), '_______________________________')}"),
            ("Téléphone", _safe(occupant.get("telephone"), "________________________________")),
            ("E-mail", _safe(occupant.get("mail"), "____________________________________________")),
        ], styles),
    ])

    story.extend([
        _heading("1. Objet de la convention", styles),
        _body(
            "La présente convention fixe les conditions dans lesquelles le Centre met "
            "temporairement à la disposition de l'Occupant une place dans son "
            "hébergement collectif situé au 54 chemin du Carreou, 83480 "
            "Puget-sur-Argens, exclusivement pour les besoins et pendant la durée de "
            "la formation identifiée en première page.",
            styles,
        ),
        _body(
            "L'hébergement est une facilité accessoire à la formation. Il est "
            "personnel, précaire, temporaire et non cessible. Il ne constitue ni un "
            "bail d'habitation principale, ni une domiciliation, ni un droit au "
            "maintien dans les lieux au-delà des dates convenues, sous réserve des "
            "dispositions impératives applicables.",
            styles,
        ),
        _heading("2. Documents contractuels et engagement de l'Occupant", styles),
        _body(
            "La convention, la fiche de remise des clés et de réception des sommes "
            "ainsi que l'attestation de prise "
            "de connaissance des règles forment un ensemble indivisible.",
            styles,
        ),
        _body(
            "En signant, l'Occupant reconnaît avoir reçu une information claire sur "
            "les horaires d'arrivée, les sommes à verser, le matériel à apporter, les "
            "équipements disponibles et l'ensemble des règles de l'hébergement. Il "
            "s'engage à les respecter pendant toute son occupation.",
            styles,
        ),
        *_chapter("Arrivée et accès au centre", styles),
        _heading("3. Durée, arrivée et remise des clés", styles),
        _subheading("3.1 - Période d'occupation", styles),
        _body(
            "La mise à disposition commence à la date et à l'heure indiquées en "
            "première page et prend fin, sans formalité particulière ni renouvellement "
            "tacite, à la date et à l'heure de départ convenues. Toute prolongation "
            "nécessite l'accord écrit préalable du Centre.",
            styles,
        ),
        _subheading("3.2 - Arrivée la veille de la formation", styles),
        _body(
            f"L'Occupant peut se présenter le <b>{_safe(arrival)}</b>, veille du premier "
            "jour de formation, impérativement entre <b>08h00 et 17h00</b>. La "
            "signature de la présente convention, le "
            "versement de la participation, la remise du chèque de caution et la "
            "remise des clés sont réalisés dans ce créneau.",
            styles,
        ),
        _callout("Aucune remise de clés après 17h00", ARRIVAL_LATE_COPY, styles, danger=True),
        _subheading("3.3 - Conditions de remise des clés", styles),
        _body("À l’accueil, indiquez vos nom, prénom et session. L’agent vérifie la réservation, "
              "note le numéro de la clé remise et complète avec vous la première annexe. "
              "Gardez votre exemplaire avec les coordonnées du centre.", styles),
        _body(
            "Les clés ne sont remises qu'après accomplissement de l'ensemble des "
            "formalités prévues au point 3.2. Elles sont strictement personnelles. "
            "Toute reproduction, prêt, transmission ou conservation après la fin de "
            "l'occupation est interdit.",
            styles,
        ),
        _heading("4. Nature et conditions d'occupation", styles),
    ])
    _bullets(story, [
        "Les stagiaires sont hébergés en dortoir collectif, sans attribution individuelle de chambre ou de lit.",
        "L'hébergement est collectif : l'Occupant accepte le partage des dortoirs, sanitaires, espaces de vie et équipements communs.",
        "L'accès est réservé aux seuls stagiaires expressément autorisés et enregistrés comme occupants.",
        "L'Occupant ne peut céder, prêter, sous-louer ou mettre sa place à disposition d'un tiers, même gratuitement.",
    ], styles)

    story.extend([
        *_chapter("Règlement du séjour", styles),
        _heading("5. Participation financière de 300 €", styles),
        _callout("Versement impératif dès l'arrivée", PARTICIPATION_COPY, styles),
        _body(
            "La participation couvre la mise à disposition de l'hébergement collectif "
            "pendant toute la période convenue, y compris les week-ends et jours fériés "
            "compris dans la session, sous réserve du respect de la présente convention.",
            styles,
        ),
        _body("Le règlement est effectué :", styles),
    ])
    _bullets(story, [
        "soit par chèque établi à l'ordre de « Intégrale Sécurité Formations » ;",
        "soit en espèces, placées dans une enveloppe fermée portant lisiblement les nom et prénom de l'Occupant.",
    ], styles)
    story.extend([
        _body(
            "Un reçu est complété dans l'annexe 1. Le caractère forfaitaire de la "
            "participation implique qu'une arrivée tardive, une absence temporaire ou "
            "un départ anticipé à l'initiative de l'Occupant n'ouvre pas "
            "automatiquement droit à une réduction, sous réserve des dispositions "
            "légales impératives et des circonstances examinées par le Centre.",
            styles,
        ),
        _subheading("Comment remplir la fiche à l’accueil", styles),
        _key_value_table([
            ("En espèces", "Inscrire le nom du stagiaire sur l’enveloppe. Cocher le mode de règlement et dater la réception dans l’annexe 1."),
            ("Par chèque", "Reporter le numéro du chèque, le nom de la banque et la date. Utiliser la rubrique distincte pour le chèque de caution."),
            ("Reçu", "Indiquer sur les deux exemplaires si un reçu a été remis, et reporter sa référence lorsqu’elle existe."),
        ], styles),
        *_chapter("Le chèque de caution", styles),
        _heading("6. Dépôt de garantie - chèque de caution de 200 €", styles),
        _callout("Remise impérative dès l'arrivée", DEPOSIT_COPY, styles),
        _body("Le chèque de caution garantit notamment :", styles),
    ])
    _bullets(story, [
        "les dégradations imputables à l'Occupant, hors vétusté ou usure normale ;",
        "la perte, la non-restitution ou la détérioration d'une clé, d'un badge ou d'un moyen d'accès ;",
        "le matériel, le mobilier ou les équipements manquants ou détériorés ;",
        "les frais de nettoyage rendus nécessaires par un état anormalement sale ou le non-respect des obligations d'entretien ;",
        "plus généralement, toute somme restant due au titre de dommages directement imputables à l'Occupant.",
    ], styles)
    story.extend([
        _body(
            "Le chèque est conservé sans encaissement, sauf nécessité de couvrir tout "
            "ou partie d'une somme justifiée. L'Occupant est informé de la nature des "
            "dommages ou manquements constatés et, lorsque cela est possible, des "
            "justificatifs ou éléments d'évaluation correspondants.",
            styles,
        ),
        _body(
            "La caution ne constitue pas un plafond de responsabilité. Si le coût "
            "justifié des dommages excède 200 €, le Centre peut demander le règlement "
            "du solde restant dû.",
            styles,
        ),
        _body(
            "Après restitution des clés, si aucune somme n'est due, le chèque est "
            "rendu à l'Occupant. Si une vérification complémentaire est nécessaire, la "
            "restitution du chèque ou du solde intervient dans un délai maximal de 30 "
            "jours calendaires après la remise des clés.",
            styles,
        ),
        _subheading("Lors de la restitution", styles),
        _body("La dernière rubrique de l’annexe 1 permet de noter la date de retour de la clé "
              "et de suivre séparément le chèque de caution. Si les deux remises n’ont pas lieu "
              "le même jour, indiquez la date réelle de chacune.", styles),
        *_chapter("Équipements et bagages", styles),
        _heading("7. Équipements mis à disposition", styles),
        _body("L'hébergement collectif comprend notamment les équipements suivants :", styles),
    ])
    _bullets(story, [
        "un dortoir ou espace de couchage collectif avec lit et matelas ;",
        "un drap-housse fourni pour le matelas ;",
        "des douches, une salle de bain et des toilettes ;",
        "une cuisine équipée et des espaces communs ;",
        "une machine à laver et un sèche-linge.",
    ], styles)
    story.extend([
        _body(
            "Les équipements sont partagés. Leur disponibilité peut être temporairement "
            "limitée pour entretien, nettoyage, réparation ou sécurité. Toute panne ou "
            "anomalie doit être signalée sans délai ; l'Occupant ne doit pas intervenir "
            "lui-même sur une installation technique.",
            styles,
        ),
    ])
    story.append(_subheading("Après avoir utilisé un espace partagé", styles))
    shared_spaces = [
        ("Cuisine", "Lavez et rangez votre vaisselle, essuyez le plan de travail et laissez l’évier libre pour la personne suivante."),
        ("Laverie", "Récupérez votre linge à la fin du cycle. Laissez le tambour vide et l’espace autour de la machine propre."),
        ("Sanitaires", "Reprenez vos affaires de toilette et laissez la douche et le lavabo propres après usage."),
    ]
    story.append(_key_value_table(shared_spaces, styles))
    story.extend([
        _heading("8. Matériel et consommables à apporter", styles),
        _body("L'Occupant doit arriver avec le matériel personnel suivant :", styles),
    ])
    _bullets(story, [
        "un sac de couchage ou une couverture ;",
        "un oreiller ;",
        "du gel douche, du savon et du shampoing ;",
        "de la lessive adaptée aux équipements mis à disposition ;",
        "des sacs-poubelle et du papier toilette d'appoint en cas de rupture entre deux passages de la société de nettoyage.",
    ], styles)
    story.extend([
        _body(
            "Le drap-housse fourni ne remplace ni le sac de couchage ou la couverture, "
            "ni l'oreiller. Le linge personnel et les produits d'hygiène restent sous "
            "la responsabilité de l'Occupant.",
            styles,
        ),
        *_chapter("La vie en dortoir", styles),
        _heading("9. Propreté, rangement et vie collective", styles),
        _subheading("9.1 - Entretien courant", styles),
    ])
    _bullets(story, [
        "Maintenir son couchage, sa zone personnelle et les espaces communs propres et rangés.",
        "Nettoyer immédiatement après usage la cuisine, la vaisselle, les sanitaires et les appareils partagés.",
        "Trier, fermer et évacuer les déchets ; ne laisser aucun aliment ou déchet susceptible de créer des odeurs ou nuisibles.",
        "Utiliser les installations, le mobilier et les appareils conformément à leur destination et aux consignes affichées.",
        "Ne pas déplacer durablement le mobilier ou les équipements sans autorisation.",
    ], styles)
    story.append(_subheading("9.2 - Comportement et tenue", styles))
    _bullets(story, [
        "Adopter en permanence un comportement courtois et respectueux envers les autres occupants, le personnel, les intervenants et le voisinage.",
        "Porter une tenue correcte dans les espaces communs ; il est interdit d'y circuler torse nu ou en sous-vêtements, avant, pendant ou après les heures de formation.",
        "Respecter l'intimité, le sommeil, les effets personnels et l'espace utilisé par les autres occupants.",
        "Tout harcèlement, menace, violence, intimidation, propos discriminatoire ou comportement dangereux est interdit.",
    ], styles)

    story.extend([
        _heading("10. Calme et respect du voisinage", styles),
        _callout(
            "Silence obligatoire après 22h00",
            "À compter de 22h00, aucun bruit ne doit être perceptible de nature à "
            "gêner les autres occupants ou le voisinage. Les fêtes, rassemblements "
            "bruyants, cris, musique amplifiée et nuisances répétées sont interdits à "
            "toute heure.",
            styles,
        ),
        _body(
            "Les appels téléphoniques, vidéos, appareils audio et alarmes doivent être "
            "utilisés à volume modéré. Chacun prend les précautions nécessaires pour "
            "préserver le repos collectif, notamment lors des départs matinaux ou "
            "retours tardifs autorisés.",
            styles,
        ),
        *_chapter("Les interdictions sur le site", styles),
        _heading("11. Interdictions essentielles", styles),
        _subheading("11.1 - Tabac et vapotage", styles),
        _body(
            "Il est strictement interdit de fumer ou de vapoter à l'intérieur des "
            "locaux, y compris dans les dortoirs, sanitaires, cuisine, couloirs et "
            "espaces communs. Les éventuelles zones extérieures autorisées doivent être "
            "utilisées proprement et sans nuisance.",
            styles,
        ),
        _subheading("11.2 - Alcool, drogues et état d'ébriété", styles),
        _body(
            "L'introduction, la détention, la distribution ou la consommation de "
            "boissons alcoolisées, de stupéfiants ou de substances illicites dans les "
            "locaux est formellement interdite. Il est également interdit de pénétrer "
            "ou de séjourner dans le Centre en état d'ivresse ou sous l'emprise de drogue.",
            styles,
        ),
        _subheading("11.3 - Personnes extérieures", styles),
        _body(
            "Aucune personne extérieure - membre de la famille, ami, connaissance ou "
            "autre tiers - ne peut être introduite ou hébergée, même temporairement. "
            "L'accès aux espaces d'hébergement est strictement réservé aux stagiaires "
            "autorisés et au personnel habilité.",
            styles,
        ),
        _subheading("11.4 - Objets et pratiques dangereux", styles),
    ])
    _bullets(story, [
        "Il est interdit d'allumer des bougies, flammes nues, encens ou appareils de cuisson hors des emplacements prévus.",
        "Les chauffages d'appoint, branchements électriques improvisés, multiprises en cascade et appareils présentant un danger sont interdits.",
        "Il est strictement interdit de brancher un véhicule électrique sur une prise du site, à l'intérieur comme à l'extérieur, y compris avec une rallonge.",
        "Il est interdit d'introduire tout objet ou produit prohibé par la loi ou susceptible de compromettre la sécurité des personnes et des biens.",
        "Les animaux ne sont pas admis, sauf obligation légale ou accord écrit préalable du Centre pour une situation particulière.",
    ], styles)
    story.extend([
        *_chapter("Sécurité et moyens d’accès", styles),
        _heading("12. Sécurité des personnes et des locaux", styles),
    ])
    _bullets(story, [
        "Prendre connaissance des issues de secours, plans d'évacuation, consignes incendie et points de rassemblement.",
        "Ne jamais obstruer une sortie, un couloir, une porte coupe-feu ou l'accès à un dispositif de secours.",
        "Ne pas neutraliser, déplacer ou manipuler sans nécessité les détecteurs, alarmes, extincteurs et équipements de sécurité.",
        "Signaler immédiatement au Centre tout accident, début d'incendie, fuite, panne dangereuse, menace ou situation anormale.",
        "En cas d'urgence, appeler les secours compétents : 18 ou 112, puis prévenir le Centre dès que possible.",
    ], styles)
    story.extend([
        _body(
            "L'Occupant respecte toute consigne complémentaire affichée ou communiquée "
            "pour des raisons de sécurité, d'hygiène, de maintenance ou de bon "
            "fonctionnement collectif.",
            styles,
        ),
        _subheading("Si une clé est perdue ou si un accès est bloqué", styles),
        _body("Prévenez l’accueil au 04 22 47 07 68 ou adressez-vous à un membre du personnel "
              "présent. Donnez votre nom, votre session et, si vous le connaissez, le numéro "
              "de la clé inscrit sur votre convention.", styles),
        _heading("13. Clés, contrôle des accès et droit d'intervention", styles),
        _body(
            "L'Occupant veille à la fermeture des accès et ne communique aucun code ni "
            "moyen d'accès. Toute perte ou anomalie est signalée immédiatement. Les "
            "frais justifiés de remplacement, de reproduction ou de sécurisation "
            "rendus nécessaires par une perte imputable à l'Occupant peuvent être mis "
            "à sa charge.",
            styles,
        ),
        _body(
            "Le Centre peut accéder aux espaces d'hébergement pour assurer la sécurité, "
            "porter secours, effectuer une réparation, prévenir un dommage, procéder à "
            "l'entretien ou vérifier le respect des règles. Sauf urgence ou "
            "impossibilité, les occupants en sont informés préalablement ou dans les "
            "meilleurs délais.",
            styles,
        ),
        *_chapter("Responsabilités pendant le séjour", styles),
        _heading("14. Responsabilité, assurance et effets personnels", styles),
        _subheading("14.1 - Responsabilité de l'Occupant", styles),
        _body(
            "L'Occupant répond des dommages directs qu'il cause aux locaux, équipements, "
            "autres occupants ou tiers par faute, négligence ou non-respect de la "
            "convention. Il signale spontanément tout dommage, même accidentel, afin de "
            "permettre sa sécurisation et son traitement.",
            styles,
        ),
        _subheading("14.2 - Assurance", styles),
        _body(
            "L'Occupant déclare être couvert par une assurance de responsabilité civile "
            "en cours de validité pour les dommages susceptibles d'être causés à des "
            "tiers. Le Centre peut demander une attestation lorsque la situation le justifie.",
            styles,
        ),
        _subheading("14.3 - Effets personnels", styles),
        _body(
            "Les effets personnels, espèces, documents, appareils électroniques et "
            "objets de valeur demeurent sous la garde de l'Occupant. Il lui appartient "
            "de les sécuriser et de ne pas les laisser sans surveillance. La "
            "responsabilité du Centre ne peut être engagée qu'en cas de faute établie "
            "ou dans les autres cas prévus par la loi.",
            styles,
        ),
        _heading("15. Manquements, mesures de protection et fin anticipée", styles),
        _body("Selon la gravité et la répétition des faits, le non-respect de la convention peut entraîner :", styles),
    ])
    _bullets(story, [
        "un rappel immédiat des règles et une demande de mise en conformité ;",
        "un avertissement écrit et, le cas échéant, la réparation ou le remboursement du dommage ;",
        "la fin anticipée de l'hébergement et l'obligation de restituer immédiatement les clés.",
    ], styles)
    story.extend([
        _body(
            "Une fin immédiate de l'hébergement peut être décidée sans avertissement "
            "préalable lorsqu'un fait grave compromet la sécurité, la tranquillité ou "
            "l'intégrité des personnes ou des biens, notamment en cas de violence, "
            "menace, alcool ou drogue, personne extérieure, dégradation volontaire, "
            "mise en danger, nuisance grave ou refus d'obtempérer à une consigne de sécurité.",
            styles,
        ),
        _callout(
            "Conséquence d'une fin anticipée",
            "L'Occupant doit alors libérer les lieux, restituer les clés et organiser "
            "à ses frais une autre solution d'hébergement. La fin de l'hébergement "
            "n'emporte pas automatiquement exclusion de la formation : toute "
            "éventuelle mesure concernant la formation relève d'une procédure "
            "distincte et des règles applicables au stagiaire.",
            styles,
            danger=True,
        ),
        *_chapter("Le départ et les démarches", styles),
        _heading("16. Départ et restitution des clés", styles),
    ])
    _bullets(story, [
        "Libérer le couchage utilisé à la date et à l'heure convenues.",
        "Retirer tous les effets personnels, denrées et déchets ; nettoyer et ranger les espaces utilisés.",
        "Restituer personnellement toutes les clés, badges et moyens d'accès au représentant désigné du Centre.",
        "Communiquer un moyen de contact valable si le retour de la caution doit intervenir après une vérification complémentaire.",
    ], styles)
    story.extend([
        _body(
            "Tout bien abandonné est traité conformément aux règles applicables. Le "
            "Centre contacte l'Occupant lorsqu'il est identifiable et peut demander le "
            "remboursement des frais justifiés de conservation ou d'expédition.",
            styles,
        ),
        _heading("17. Données personnelles", styles),
        _body(
            "Les données recueillies dans la convention sont utilisées par le Centre "
            "pour gérer l'attribution de l'hébergement, les accès, les paiements, la "
            "caution, la sécurité et les éventuels incidents ou "
            "litiges. Elles sont accessibles aux seules personnes habilitées et "
            "conservées pendant la durée nécessaire à ces finalités et au respect des "
            "obligations légales.",
            styles,
        ),
        _body(
            "L'Occupant peut exercer ses droits d'accès, de rectification, d'effacement, "
            "de limitation ou d'opposition, dans les conditions prévues par la "
            "réglementation, en écrivant au siège d'Intégrale Sécurité Formations. Il "
            "peut également saisir la CNIL.",
            styles,
        ),
        _heading("18. Réclamations, droit applicable et litiges", styles),
        _body(
            "La convention est soumise au droit français. Toute difficulté fait "
            "d'abord l'objet d'une tentative de résolution amiable avec le Centre. À "
            "défaut d'accord, chaque partie conserve la possibilité d'exercer les "
            "recours prévus par la loi devant la juridiction territorialement et "
            "matériellement compétente selon les règles de droit commun.",
            styles,
        ),
        _body(
            "Si une stipulation est déclarée nulle ou inapplicable, les autres "
            "stipulations demeurent applicables dans toute la mesure permise par la loi.",
            styles,
        ),
        *_chapter("Signature de la convention", styles),
        _heading("19. Acceptation et signature", styles),
        _body(
            "L'Occupant reconnaît avoir lu la convention et ses annexes, avoir pu "
            "demander toute explication utile et accepter sans réserve les obligations "
            "qui y sont prévues. Il reconnaît notamment que les interdictions relatives "
            "au bruit, aux personnes extérieures, au tabac, au vapotage, à l'alcool et "
            "aux drogues constituent des conditions essentielles de l'hébergement.",
            styles,
        ),
        _body("La convention et ses annexes sont établies en deux exemplaires. "
              "Un exemplaire signé est remis à l’Occupant, l’autre est conservé par le Centre.", styles),
        _key_value_table([
            ("Occupant", _safe(occupant_name)),
            ("Session", _safe(session)),
            ("Documents à remettre", "Convention d’hébergement, fiche de remise de la clé et des sommes, attestation de prise de connaissance des règles."),
        ], styles),
        Spacer(1, 9 * mm),
        _body(f"<b>Fait à Puget-sur-Argens, le</b> {_safe(contract_date)} &nbsp;&nbsp; <b>à</b> {_safe(contract_time)}", styles),
        _signature_table(occupant_name, "Faire précéder la signature manuscrite de « Lu et approuvé ».", centre_visa, styles),
        PageBreak(),
    ])

    story.extend(_annex_header(
        "1",
        "REMISE DE LA CLÉ ET DES SOMMES",
        "À compléter impérativement lors de l'arrivée, avant l'accès à l'hébergement",
        styles,
    ))
    story.extend([
        _key_value_table([
            ("Occupant", _safe(occupant_name)),
            ("Session de formation", _safe(session)),
            ("Arrivée", f"{_safe(arrival)} - Heure : {_safe(arrival_time)}"),
            ("Clé d’entrée n°", key_number),
            ("Agent d’accueil présent", _safe(reception_agent, "________________________________")),
        ], styles),
        _heading("A. Participation financière - 300 €", styles),
        _body(
            "À la remise des clés, cochez la case ci-dessous pour confirmer le règlement.",
            styles,
        ),
    ])
    def checkbox(checked):
        return "[X]" if checked else "[&nbsp;&nbsp;]"

    payment_method = str(context.get("payment_method") or "")
    cash = checkbox(payment_method == "Espèces")
    cheque = checkbox(payment_method == "Chèque")
    receipt = context.get("receipt_issued")
    payment_rows = [
        ("Mode de règlement", f"{cash} Espèces &nbsp;&nbsp;&nbsp; {cheque} Chèque"),
        ("Participation reçue", f"{checkbox(context.get('payment_status') == 'Payé')} 300 € &nbsp;&nbsp; le {_safe(context.get('payment_date'), '____ / ____ / ______')}"),
    ]
    if payment_method != "Espèces":
        payment_rows.append((
            "Si paiement par chèque",
            f"N° {_safe(context.get('payment_cheque_number'), '_______________')} &nbsp; "
            f"Banque : {_safe(context.get('payment_bank'), '______________________')}<br/>"
            f"Date du chèque : {_safe(context.get('payment_cheque_date'), '____ / ____ / ______')}",
        ))
    payment_rows.append((
        "Reçu remis",
        f"{checkbox(receipt == 'Oui')} Oui &nbsp;&nbsp; {checkbox(receipt == 'Non')} Non"
        f" &nbsp;&nbsp; Référence : {_safe(context.get('receipt_reference'), '_______________')}",
    ))
    story.extend([
        _key_value_table(payment_rows, styles),
        _heading("B. Dépôt de garantie - chèque de caution de 200 €", styles),
        _body(
            "Cochez la case ci-dessous à la réception du chèque de caution.",
            styles,
        ),
        _key_value_table([
            ("Titulaire du compte", _safe(context.get("deposit_holder"), "________________________________")),
            ("Banque", _safe(context.get("deposit_bank"), "________________________________")),
            ("N° du chèque", _safe(context.get("deposit_cheque_number"), "________________________")),
            ("Date du chèque", _safe(context.get("deposit_cheque_date"), "____ / ____ / ______")),
            ("Chèque reçu", f"{checkbox(context.get('deposit_received') == 'Oui')} 200 € - deux cents euros"),
        ], styles),
        PageBreak(),
        *_annex_header("1", "Remise de la clé et des documents", f"{occupant_name} · {reference}", styles),
        _heading("C. À la remise de la clé", styles),
    ])
    handover = context.get("handover_checklist") or {}
    def mark(key):
        return checkbox(handover.get(key))

    checklist = Table([
        [Paragraph(f"{mark('convention_reviewed')} Convention relue avec l'Occupant", styles["Cell"]), Paragraph(f"{mark('copy_delivery_planned')} Copie prévue pour l'Occupant", styles["Cell"])],
        [Paragraph(f"{mark('rules_explained')} Règles essentielles expliquées", styles["Cell"]), ""],
        [Paragraph(f"{mark('key_handed_over')} Clé / badge remis", styles["Cell"]), Paragraph(f"{mark('safety_explained')} Consignes de sécurité et issues de secours indiquées", styles["Cell"])],
    ], colWidths=[84 * mm, 84 * mm], hAlign="LEFT")
    checklist.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([
        checklist,
        Spacer(1, 6 * mm),
        _signature_table(occupant_name, "Signature manuscrite à la remise des sommes, des documents et de la clé.", centre_visa, styles),
        Spacer(1, 6 * mm),
        _heading("D. Au départ : retour de la clé et de la caution", styles),
        _body("À remplir au moment de chaque remise. Les signatures ci-dessus concernent l’arrivée.", styles),
        _key_value_table([
            ("Clé restituée", "N° ___________________    Date : ____ / ____ / ______<br/>Reçue par : ___________________________________"),
            ("Chèque de caution", "[&nbsp;&nbsp;] Rendu le ____ / ____ / ______<br/>"
                                 "[&nbsp;&nbsp;] À restituer ultérieurement<br/>"
                                 "[&nbsp;&nbsp;] Retenue à préciser selon l’article 6"),
            ("Suivi de la caution", "Remis à : ___________________________________<br/>"
                                   "Date et suite donnée : __________________________<br/>"
                                   "____________________________________________"),
        ], styles),
        PageBreak(),
    ])

    story.extend(_annex_header(
        "2",
        "ATTESTATION DE PRISE DE CONNAISSANCE DES RÈGLES",
        "Chaque engagement ci-dessous constitue une condition de l'hébergement collectif",
        styles,
    ))
    story.append(_body(
        f"Je soussigné(e), <b>{_safe(occupant_name)}</b>, inscrit(e) à la formation "
        f"<b>{_safe(formation)}</b>, session <b>{_safe(session)}</b>, reconnais avoir "
        "reçu, lu et compris la convention d'hébergement et m'engage expressément à "
        "respecter les règles suivantes :",
        styles,
    ))
    acknowledgements = [
        ("Arrivée et signature", f"Je me présente le {arrival}, entre 08h00 et 17h00. Je signe la convention avant la remise des clés. Aucun accès n'est possible après 17h00."),
        ("Participation de 300 €", "Je verse impérativement les 300 € dès mon arrivée, lors de la signature de la convention et de la remise des clés, par chèque ou en espèces dans une enveloppe à mon nom."),
        ("Caution de 200 €", "Je remets impérativement dès mon arrivée un chèque de caution distinct de 200 € destiné notamment à couvrir les dégradations, pertes de clés et matériels manquants."),
        ("Matériel personnel", "J'apporte mon sac de couchage ou ma couverture, mon oreiller, mes produits d'hygiène, ma lessive ainsi que les consommables d'appoint nécessaires."),
        ("Propreté", "Je maintiens mon espace et les parties communes propres et rangés et je nettoie les équipements après utilisation."),
        ("Tenue et respect", "J'adopte une tenue correcte, je ne circule pas torse nu ou en sous-vêtements et je respecte les autres occupants, le personnel et le voisinage."),
        ("Calme", "Je ne fais aucun bruit après 22h00 et je n'organise aucune fête ni rassemblement bruyant."),
        ("Tabac et vapotage", "Je ne fume pas et ne vapote pas à l'intérieur des locaux."),
        ("Alcool et drogues", "Je n'introduis, ne détiens et ne consomme aucun alcool ni drogue dans les locaux et je n'y entre pas sous leur emprise."),
        ("Personnes extérieures", "Je ne fais entrer et n'héberge aucune personne extérieure, y compris famille, amis ou connaissances."),
        ("Sécurité", "Je respecte les consignes incendie, les issues de secours, les équipements de sécurité et je signale immédiatement tout incident ou dommage."),
        ("Véhicule électrique", "Je ne branche aucun véhicule électrique sur les prises du site, même avec une rallonge."),
        ("Départ", "Je nettoie et libère les lieux à la date convenue et restitue personnellement toutes les clés et badges."),
        ("Sanctions", "Je comprends qu'un manquement grave ou répété peut entraîner la fin immédiate de l'hébergement et l'obligation de trouver une autre solution à mes frais."),
    ]
    story.append(_acknowledgement_table(acknowledgements[:7], styles))
    story.append(PageBreak())
    story.extend(_annex_header("2", "Sécurité et départ", f"{occupant_name} · {reference}", styles))
    story.extend([
        _acknowledgement_table(acknowledgements[7:], styles),
        _callout("Déclaration finale", FINAL_ACKNOWLEDGEMENT_COPY, styles),
        _body(
            f"<b>Fait à Puget-sur-Argens, le</b> {_safe(contract_date)} "
            f"<b>à</b> {_safe(contract_time)}",
            styles,
        ),
        _signature_table(occupant_name, "Faire précéder la signature manuscrite de « Lu et approuvé ».", centre_visa, styles),
    ])

    def page_decorator(canvas, document):
        _draw_header_footer(canvas, document, reference)

    doc.build(story, onFirstPage=page_decorator, onLaterPages=page_decorator)
    return output.getvalue()
