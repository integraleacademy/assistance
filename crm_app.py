"""Point d'entrée Gunicorn enrichi des extensions CRM isolées."""

from app import app
from crm_salesforce_import import register_salesforce_import

register_salesforce_import(app)
