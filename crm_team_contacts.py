"""Professional contact details shared by the secretariat and callback replies."""

import re
import unicodedata


# Existing, client-facing coordinates from the secretariat directory.
# Never derive this directory from user accounts, CRM contacts or internal notes.
TEAM_CONTACTS = (
    {"id": "aurelie", "name": "Aurélie CHAUSSEZ", "role": "Relations clients et BTS",
     "direct": "04 87 83 06 15", "mobile": "07 69 39 04 57", "email": "aurelie@integraleacademy.com"},
    {"id": "cassandre", "name": "Cassandre MENARD", "role": "Responsable commerciale",
     "direct": "04 87 83 06 16", "mobile": "07 43 58 22 64", "email": "cassandre@integraleacademy.com"},
)


def reply_team_contacts(instructions):
    """Give the writer only the approved people named in the team's instructions."""
    normalized = "".join(char for char in unicodedata.normalize("NFKD", instructions.casefold())
                         if not unicodedata.combining(char))
    return [{"nom": person["name"], "fonction": person["role"],
             "telephone_direct": person["direct"], "telephone_portable": person["mobile"],
             "email": person["email"]}
            for person in TEAM_CONTACTS
            if re.search(r"\b" + re.escape(person["id"]) + r"\b", normalized)]
