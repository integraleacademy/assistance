"""Compatibilité étendue pour les exports CSV Salesforce.

Salesforce et Excel peuvent produire des fichiers UTF-16 tabulés, avec une
ligne ``sep=;``, un préambule de rapport ou des libellés de colonnes français.
Le parseur historique retombait alors sur Windows-1252 et ne reconnaissait
aucune colonne. Ce module remplace uniquement la lecture CSV, sans modifier la
logique métier d'import et de fusion des contacts.
"""

from __future__ import annotations

import codecs
import csv
import io
import re
import unicodedata
from typing import Any, Dict, List, Tuple


HEADER_ALIASES = {
    # Identité Salesforce
    "id": "Id",
    "lead id": "Id",
    "record id": "Id",
    "salesforce id": "Id",
    "id piste": "Id",
    "id de la piste": "Id",
    "identifiant piste": "Id",
    "identifiant de la piste": "Id",
    "identifiant du lead": "Id",
    "id de l enregistrement": "Id",
    "identifiant de l enregistrement": "Id",
    "firstname": "FirstName",
    "first name": "FirstName",
    "prenom": "FirstName",
    "prenom de la piste": "FirstName",
    "prenom du lead": "FirstName",
    "lastname": "LastName",
    "last name": "LastName",
    "nom": "LastName",
    "nom de famille": "LastName",
    "nom de la piste": "LastName",
    "nom du lead": "LastName",
    "name": "Name",
    "nom complet": "Name",
    "nom complet de la piste": "Name",
    # Champs Salesforce standards
    "email": "Email",
    "e mail": "Email",
    "adresse e mail": "Email",
    "phone": "Phone",
    "telephone": "Phone",
    "telephone professionnel": "Phone",
    "mobilephone": "MobilePhone",
    "mobile phone": "MobilePhone",
    "telephone mobile": "MobilePhone",
    "portable": "MobilePhone",
    "company": "Company",
    "societe": "Company",
    "status": "Status",
    "statut": "Status",
    "isconverted": "IsConverted",
    "converti": "IsConverted",
    "est converti": "IsConverted",
    "isdeleted": "IsDeleted",
    "supprime": "IsDeleted",
    "createddate": "CreatedDate",
    "date de creation": "CreatedDate",
    "lastmodifieddate": "LastModifiedDate",
    "date de derniere modification": "LastModifiedDate",
    "leadsource": "LeadSource",
    "source de la piste": "LeadSource",
    "description": "Description",
    # Champs personnalisés utilisés par le CRM
    "type de formation": "Type_de_formation__c",
    "formation": "Type_de_formation__c",
    "lieu": "Lieu__c",
    "dates souhaitees": "Dates_souhait_es__c",
    "date souhaitee": "Dates_souhait_es__c",
    "compte cpf": "Compte_CPF__c",
    "carte professionnelle": "Carte_prof__c",
    "carte prof": "Carte_prof__c",
    "antecedents": "Ant_c_dents__c",
    "antecedents judiciaires": "Ant_c_dents__c",
    "choix dirigeant desp": "CHOIX_DIRIGEANT_DESP__c",
    "creation identite numerique": "Cr_ation_identit_num_rique__c",
    "identite numerique fonctionnelle": "Identit_num_rique_fonctionnelle__c",
    "souhaite demande financement ft": "Souhaite_demande_financement_FT__c",
    "souhaite un financement ft": "Souhaite_demande_financement_FT__c",
    "financement personnel": "Financement_personnel__c",
    "si refus france travail": "Si_refus_France_Travail__c",
    "origine": "Origine__c",
    "inscrit france travail": "Inscrit_France_Travail__c",
    "informations complementaires": "Infos_compl_mentaires__c",
    "infos complementaires": "Infos_compl_mentaires__c",
}

KNOWN_CANONICAL_HEADERS = set(HEADER_ALIASES.values())
IDENTITY_HEADERS = {"Id", "FirstName", "LastName", "Name"}
DELIMITERS = (",", ";", "\t", "|")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _fold(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", _text(value))
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    normalized = normalized.casefold()
    normalized = re.sub(r"[\x00-\x1f]+", " ", normalized)
    normalized = re.sub(r"[_:./\\()\[\]{}-]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _canonical_header(value: Any) -> str:
    raw = _text(value).lstrip("\ufeff")
    folded = _fold(raw)
    if folded in HEADER_ALIASES:
        return HEADER_ALIASES[folded]

    # Les exports de rapports peuvent préfixer les colonnes par « Piste : ».
    for prefix in ("piste ", "lead ", "prospect ", "piste de vente "):
        if folded.startswith(prefix):
            reduced = folded[len(prefix):].strip()
            if reduced in HEADER_ALIASES:
                return HEADER_ALIASES[reduced]

    # Certains libellés contiennent aussi le nom API entre parenthèses.
    compact = re.sub(r"\s+", "", folded)
    for token, canonical in {
        "firstname": "FirstName",
        "lastname": "LastName",
        "mobilephone": "MobilePhone",
        "createddate": "CreatedDate",
        "lastmodifieddate": "LastModifiedDate",
        "leadsource": "LeadSource",
        "isconverted": "IsConverted",
        "isdeleted": "IsDeleted",
    }.items():
        if token in compact:
            return canonical

    return raw


def _decode_csv(raw: bytes) -> str:
    """Décode notamment les CSV UTF-16 qu'un décodage CP1252 accepte à tort."""
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return raw.decode("utf-32")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")

    # Un fichier UTF-16 sans BOM contient généralement un octet nul sur deux.
    if raw.count(b"\x00") > max(4, len(raw) // 20):
        odd_nulls = raw[1::2].count(0)
        even_nulls = raw[0::2].count(0)
        encodings = (
            ("utf-16-le", "utf-16-be")
            if odd_nulls >= even_nulls
            else ("utf-16-be", "utf-16-le")
        )
        for encoding in encodings:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

    errors: List[str] = []
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise ValueError("Encodage CSV illisible : " + " | ".join(errors[:2]))


def _candidate_rows(text: str) -> List[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and re.fullmatch(r"\s*sep\s*=\s*[,;\t|]\s*", lines[0], flags=re.I):
        lines = lines[1:]
    return lines


def _find_header(text: str) -> Tuple[List[str], int, str, List[str]]:
    """Repère le vrai en-tête, même après un titre de rapport Salesforce."""
    lines = _candidate_rows(text)
    best: Tuple[Tuple[int, int], int, str, List[str]] | None = None

    for delimiter in DELIMITERS:
        for index, line in enumerate(lines[:25]):
            try:
                cells = next(csv.reader([line], delimiter=delimiter))
            except csv.Error:
                continue
            canonical = [_canonical_header(cell) for cell in cells]
            identity_count = sum(item in IDENTITY_HEADERS for item in canonical)
            known_count = sum(item in KNOWN_CANONICAL_HEADERS for item in canonical)
            score = (identity_count * 20 + known_count, len(cells))
            if best is None or score > best[0]:
                best = (score, index, delimiter, canonical)

    if best is None or best[0][0] < 20:
        preview = " | ".join(line[:160] for line in lines[:3] if line.strip())
        raise ValueError(
            "Impossible d’identifier les colonnes du fichier Salesforce. "
            f"Début du fichier détecté : {preview or 'aucun contenu lisible'}"
        )
    return lines, best[1], best[2], best[3]


def _split_full_name(value: Any) -> Tuple[str, str]:
    parts = _text(value).split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def parse_salesforce_csv(raw: bytes, *, max_csv_bytes: int) -> List[Dict[str, str]]:
    if not raw:
        raise ValueError("Le fichier CSV est vide.")
    if len(raw) > max_csv_bytes:
        raise ValueError("Le fichier dépasse la limite de 20 Mo.")

    decoded = _decode_csv(raw).replace("\x00", "")
    lines, header_index, delimiter, _ = _find_header(decoded)
    body = "\n".join(lines[header_index:])
    reader = csv.DictReader(io.StringIO(body, newline=""), delimiter=delimiter)
    original_columns = reader.fieldnames or []
    column_names = {
        column: _canonical_header(column)
        for column in original_columns
        if column is not None
    }
    canonical_columns = set(column_names.values())

    # L'identifiant Salesforce et le prénom ne sont pas indispensables pour
    # créer une piste. Le nom (ou le nom complet) reste le minimum requis.
    if "LastName" not in canonical_columns and "Name" not in canonical_columns:
        detected = ", ".join(_text(column) for column in original_columns[:12])
        raise ValueError(
            "Colonne de nom Salesforce introuvable. "
            f"Colonnes détectées : {detected or 'aucune'}"
        )

    rows: List[Dict[str, str]] = []
    for source_row in reader:
        if not isinstance(source_row, dict):
            continue
        row: Dict[str, str] = {}
        for source_key, source_value in source_row.items():
            if source_key is None:
                continue
            canonical_key = column_names.get(source_key, _canonical_header(source_key))
            value = _text(source_value)
            # En cas de colonnes synonymes, conserver la première valeur utile.
            if value or canonical_key not in row:
                row[canonical_key] = value

        if "LastName" not in row and row.get("Name"):
            first_name, last_name = _split_full_name(row["Name"])
            row.setdefault("FirstName", first_name)
            row["LastName"] = last_name
        row.setdefault("FirstName", "")
        row.setdefault("Id", "")

        if any(_text(value) for value in row.values()):
            rows.append(row)

    if not rows:
        raise ValueError("Le fichier Salesforce ne contient aucune piste exploitable.")
    return rows


def install_salesforce_csv_compat(salesforce_import_module) -> None:
    """Remplace le parseur appelé par la route déjà enregistrée."""

    def compatible_parser(raw: bytes):
        return parse_salesforce_csv(
            raw,
            max_csv_bytes=int(salesforce_import_module.MAX_CSV_BYTES),
        )

    salesforce_import_module._decode_csv = _decode_csv
    salesforce_import_module.parse_salesforce_csv = compatible_parser
