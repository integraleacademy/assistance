import datetime

import app as application


def test_extracts_exam_date_from_explicit_exam_label():
    assert application._parse_exam_date_from_dates_txt(
        "Du 9 novembre 2026 au 19 janvier 2027 - examen le 20 janvier 2027"
    ) == "2027-01-20"


def test_uses_session_end_when_custom_quote_has_no_exam_label():
    assert application._parse_exam_date_from_dates_txt(
        "Du 9 novembre 2026 au 19 janvier 2027"
    ) == "2027-01-19"


def test_custom_quote_session_end_produces_monthly_payments(monkeypatch):
    class FixedDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 7)

    monkeypatch.setattr(application.datetime, "date", FixedDate)

    simulation = application.compute_plan_financement_simulation(
        formation="A3P",
        dates_txt="Du 9 novembre 2026 au 19 janvier 2027",
        cpf_value=0,
        france_travail="NON",
        date_examen_str="",
    )

    assert [payment["date"] for payment in simulation["echeances"]] == [
        "05/09/2026",
        "05/10/2026",
        "05/11/2026",
        "05/12/2026",
        "05/01/2027",
    ]
    assert sum(float(payment["montant"]) for payment in simulation["echeances"]) == 4200
    assert simulation["echeancier_message"] == ""
