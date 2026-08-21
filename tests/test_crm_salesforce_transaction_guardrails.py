from crm_salesforce_transaction_guardrails import serialize_salesforce_writes


class FakeApp:
    def __init__(self, view):
        self.view_functions = {"crm_migrate_salesforce": view}


class FakeRequest:
    def __init__(self, dry_run):
        self.form = {"dry_run": dry_run}


class CountingLock:
    def __init__(self):
        self.entries = 0
        self.exits = 0

    def __enter__(self):
        self.entries += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exits += 1


def test_preview_does_not_block_other_crm_writes():
    calls = []
    app = FakeApp(lambda: calls.append("preview") or "ok")
    lock = CountingLock()

    serialize_salesforce_writes(
        app,
        request=FakeRequest("1"),
        transaction_lock=lock,
    )

    assert app.view_functions["crm_migrate_salesforce"]() == "ok"
    assert calls == ["preview"]
    assert lock.entries == 0
    assert lock.exits == 0


def test_final_import_uses_the_shared_crm_transaction_lock():
    calls = []
    app = FakeApp(lambda: calls.append("import") or "ok")
    lock = CountingLock()

    serialize_salesforce_writes(
        app,
        request=FakeRequest("0"),
        transaction_lock=lock,
    )

    assert app.view_functions["crm_migrate_salesforce"]() == "ok"
    assert calls == ["import"]
    assert lock.entries == 1
    assert lock.exits == 1


def test_transaction_guardrail_is_installed_only_once():
    app = FakeApp(lambda: "ok")
    lock = CountingLock()
    request = FakeRequest("0")

    serialize_salesforce_writes(
        app,
        request=request,
        transaction_lock=lock,
    )
    first_wrapper = app.view_functions["crm_migrate_salesforce"]
    serialize_salesforce_writes(
        app,
        request=request,
        transaction_lock=lock,
    )

    assert app.view_functions["crm_migrate_salesforce"] is first_wrapper
    assert first_wrapper() == "ok"
    assert lock.entries == 1
