import datetime

import app as application


def test_information_form_sessions_exclude_sessions_that_already_started(monkeypatch):
    data_store = {
        "formation_sessions": {
            "cote_azur": {
                "APS": [
                    {"label": "Du 8 juin au 4 août 2026", "badge": ""},
                    {"label": "Du 3 août au 12 août 2026", "badge": ""},
                    {"label": "Du 4 août au 12 août 2026", "badge": ""},
                ]
            }
        }
    }
    upcoming = application.get_upcoming_formation_sessions(
        data_store, today=datetime.date(2026, 8, 3)
    )
    monkeypatch.setattr(application, "load_data", lambda: data_store)
    monkeypatch.setattr(application, "get_upcoming_formation_sessions", lambda store: upcoming)

    page = application.app.test_client().get(
        "/demande-informations-formations"
    ).get_data(as_text=True)

    labels = [row["label"] for row in upcoming["cote_azur"]["APS"]]
    assert labels == [
        "Du 3 août au 12 août 2026",
        "Du 4 août au 12 août 2026",
    ]
    assert "Du 3 ao\\u00fbt au 12 ao\\u00fbt 2026" in page


def test_upcoming_sessions_understand_start_dates_with_inherited_month_and_year():
    sessions = application.get_upcoming_formation_sessions(
        {
            "formation_sessions": {
                "cote_azur": {
                    "SSIAP": [
                        {"label": "Du 12 au 27 octobre 2026", "badge": ""},
                    ]
                }
            }
        },
        today=datetime.date(2026, 10, 13),
    )

    assert sessions["cote_azur"]["SSIAP"] == []


def test_session_start_date_infers_previous_year_for_cross_year_session():
    assert application._session_start_date(
        "Du 9 novembre au 19 janvier 2027"
    ) == datetime.date(2026, 11, 9)


def test_automatic_email_lists_only_training_sessions_that_have_not_started():
    html = application._format_upcoming_sessions_for_email(
        "cote_azur",
        "A3P",
        {
            "formation_sessions": {
                "cote_azur": {
                    "A3P": [
                        {"label": "Du 1er septembre au 27 octobre 2026", "badge": ""},
                        {"label": "Du 9 novembre 2026 au 19 janvier 2027", "badge": ""},
                        {"label": "Du 15 février au 26 mars 2027", "badge": ""},
                    ]
                }
            }
        },
        today=datetime.date(2026, 10, 28),
    )

    assert "Du 1er septembre au 27 octobre 2026" not in html
    assert "Du 9 novembre 2026 au 19 janvier 2027" in html
    assert "Du 15 février au 26 mars 2027" in html
