from crm_salesforce_chatter_support import *


def import_salesforce_chatter_rows(
    contacts: list[dict[str, Any]],
    publication_rows: list[dict[str, Any]],
    comment_rows: list[dict[str, Any]],
    user_rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        contacts = copy.deepcopy(contacts)
    now = dt.datetime.now(_PARIS_TZ).isoformat()
    batch_id = hashlib.sha256(now.encode()).hexdigest()[:16]
    authors = _author_map(user_rows)
    comments_by_feed, comment_stats = _prepare_comments(comment_rows, authors)
    publications, publication_stats = _prepare_publications(
        publication_rows, authors, comments_by_feed,
    )
    contacts_by_sf = _contact_index(contacts)
    contacts_by_task = _task_contact_index(contacts)
    counts: Counter[str] = Counter()
    per_contact: dict[str, Counter[str]] = defaultdict(Counter)
    ready: dict[str, dict[str, Any]] = {}
    unmatched: dict[str, dict[str, Any]] = {}
    ambiguous: dict[str, dict[str, Any]] = {}

    for incoming in publications:
        parent_key = _sf_key(incoming.get("salesforce_parent_id"))
        candidates = (
            contacts_by_sf if _is_lead(parent_key) else contacts_by_task
        ).get(parent_key, [])
        if not candidates:
            counts["unmatched_publications"] += 1
            row = unmatched.setdefault(parent_key, {
                "salesforce_parent_id": _text(incoming.get("salesforce_parent_id")),
                "parent_type": "Piste" if _is_lead(parent_key) else "Tâche",
                "publication_count": 0,
                "comment_count": 0,
            })
            row["publication_count"] += 1
            row["comment_count"] += len(incoming.get("comments") or [])
            continue
        if len(candidates) > 1:
            counts["ambiguous_publications"] += 1
            row = ambiguous.setdefault(parent_key, {
                "salesforce_parent_id": _text(incoming.get("salesforce_parent_id")),
                "parent_type": "Piste" if _is_lead(parent_key) else "Tâche",
                "crm_contacts": [_contact_name(contact) for contact in candidates],
                "publication_count": 0,
                "comment_count": 0,
            })
            row["publication_count"] += 1
            row["comment_count"] += len(incoming.get("comments") or [])
            continue

        contact = candidates[0]
        contact_id = _text(contact.get("id"))
        current = _publication_index(contact).get(
            incoming["salesforce_feed_item_id"]
        )
        changed = False
        comments_created = comments_updated = 0
        if current is None:
            contact.setdefault("salesforce_chatter", []).append(
                copy.deepcopy(incoming)
            )
            counts["publications_created"] += 1
            comments_created = len(incoming.get("comments") or [])
            counts["comments_created"] += comments_created
            changed = True
        else:
            (
                changed,
                comments_created,
                comments_updated,
                comments_unchanged,
            ) = _merge_publication(current, incoming)
            counts["comments_created"] += comments_created
            counts["comments_updated"] += comments_updated
            counts["comments_unchanged"] += comments_unchanged
            counts[
                "publications_updated" if changed else "publications_unchanged"
            ] += 1

        per_contact[contact_id]["publications"] += 1
        per_contact[contact_id]["comments"] += len(incoming.get("comments") or [])
        if changed or comments_created or comments_updated:
            per_contact[contact_id]["changed"] += 1
            contact["salesforce_chatter_import_batch_id"] = batch_id
            contact["salesforce_chatter_imported_at"] = now
            contact["updated_at"] = now

        row = ready.setdefault(contact_id, {
            "contact_id": contact_id,
            "person": _contact_name(contact) or "Fiche sans nom",
            "formation": _text(contact.get("formation")),
            "salesforce_ids": sorted(_contact_sf_keys(contact)),
            "publications": 0,
            "comments": 0,
        })
        row["publications"] += 1
        row["comments"] += len(incoming.get("comments") or [])

    for contact in contacts:
        contact_id = _text(contact.get("id"))
        if contact_id not in per_contact:
            continue
        chatter = [
            row
            for row in (contact.get("salesforce_chatter") or [])
            if isinstance(row, dict)
        ]
        chatter.sort(
            key=lambda row: (_text(row.get("date")), _text(row.get("id"))),
            reverse=True,
        )
        contact["salesforce_chatter"] = chatter
        contact["salesforce_chatter_count"] = len(chatter)
        contact["salesforce_chatter_comment_count"] = sum(
            len(row.get("comments") or []) for row in chatter
        )

    counts["matched_publications"] = sum(
        values["publications"] for values in per_contact.values()
    )
    counts["matched_comments"] = sum(
        values["comments"] for values in per_contact.values()
    )
    counts["matched_contacts"] = len(per_contact)
    counts["contacts_updated"] = sum(
        1 for values in per_contact.values() if values["changed"]
    )

    return {
        "ok": True,
        "dry_run": dry_run,
        "batch_id": batch_id,
        "user_rows": len(user_rows),
        "authors_resolved": len(authors),
        "publication_csv_rows": len(publication_rows),
        "comment_csv_rows": len(comment_rows),
        "matched_contacts": counts["matched_contacts"],
        "contacts_updated": counts["contacts_updated"],
        "matched_publications": counts["matched_publications"],
        "matched_comments": counts["matched_comments"],
        "publications_created": counts["publications_created"],
        "publications_updated": counts["publications_updated"],
        "publications_unchanged": counts["publications_unchanged"],
        "comments_created": counts["comments_created"],
        "comments_updated": counts["comments_updated"],
        "comments_unchanged": counts["comments_unchanged"],
        "unmatched_publications": counts["unmatched_publications"],
        "ambiguous_publications": counts["ambiguous_publications"],
        "unmatched_parent_count": len(unmatched),
        "ambiguous_parent_count": len(ambiguous),
        "ready_rows": sorted(
            ready.values(),
            key=lambda row: (
                -int(row.get("publications") or 0),
                row.get("person") or "",
            ),
        ),
        "unmatched_rows": sorted(
            unmatched.values(),
            key=lambda row: -int(row.get("publication_count") or 0),
        ),
        "ambiguous_rows": sorted(
            ambiguous.values(),
            key=lambda row: -int(row.get("publication_count") or 0),
        ),
        **comment_stats,
        **publication_stats,
    }


def _contacts_signature(contacts: list[dict[str, Any]]) -> str:
    payload: list[Any] = []
    for contact in sorted(contacts, key=lambda row: _text(row.get("id"))):
        keys = sorted(_contact_sf_keys(contact))
        task_keys = sorted(
            _sf_key(relance.get("salesforce_task_id"))
            for relance in (contact.get("relances") or [])
            if isinstance(relance, dict)
            and _sf_key(relance.get("salesforce_task_id"))
        )
        if not keys and not task_keys:
            continue
        chatter = []
        for item in contact.get("salesforce_chatter") or []:
            if not isinstance(item, dict):
                continue
            chatter.append((
                _text(item.get("salesforce_feed_item_id")),
                _text(item.get("date")),
                _text(item.get("texte")),
                tuple(sorted(
                    _text(comment.get("salesforce_feed_comment_id"))
                    for comment in (item.get("comments") or [])
                    if isinstance(comment, dict)
                    and _text(comment.get("salesforce_feed_comment_id"))
                )),
            ))
        payload.append((
            _text(contact.get("id")),
            _text(contact.get("updated_at")),
            keys,
            task_keys,
            sorted(chatter),
        ))
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _preview_token(
    publication_raw: bytes,
    comment_raw: bytes,
    user_raw: bytes,
    contacts: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    for raw in (publication_raw, comment_raw, user_raw):
        digest.update(hashlib.sha256(raw).digest())
    digest.update(_contacts_signature(contacts).encode())
    return digest.hexdigest()


def register_salesforce_chatter_import(
    app,
    *,
    current_user_fn,
    load_data_fn,
    login_required_fn,
    save_data_fn,
    transaction_lock=None,
) -> None:
    endpoint = "crm_import_salesforce_chatter"
    if endpoint in app.view_functions:
        return
    from flask import jsonify, request

    shared_lock = transaction_lock or _IMPORT_LOCK

    @app.post("/api/crm/import-salesforce-chatter", endpoint=endpoint)
    @login_required_fn
    def crm_import_salesforce_chatter():
        if (current_user_fn() or {}).get("role") != "admin":
            return jsonify({
                "error": (
                    "Seul un administrateur peut importer l'historique "
                    "Salesforce."
                )
            }), 403
        publications_file = request.files.get("publications_file")
        comments_file = request.files.get("comments_file")
        users_file = request.files.get("users_file")
        if not publications_file or not publications_file.filename:
            return jsonify({
                "error": "Sélectionnez le fichier FeedItem des publications."
            }), 400
        if not comments_file or not comments_file.filename:
            return jsonify({
                "error": "Sélectionnez le fichier FeedComment des commentaires."
            }), 400
        if not users_file or not users_file.filename:
            return jsonify({
                "error": "Sélectionnez le fichier User des utilisateurs."
            }), 400

        dry_run = _text(request.form.get("dry_run")) == "1"
        supplied_token = _text(request.form.get("preview_token"))
        try:
            publications_raw = publications_file.read(
                MAX_PUBLICATIONS_BYTES + 1
            )
            comments_raw = comments_file.read(MAX_COMMENTS_BYTES + 1)
            users_raw = users_file.read(MAX_USERS_BYTES + 1)
            publication_rows = parse_salesforce_publications_csv(
                publications_raw
            )
            comment_rows = parse_salesforce_comments_csv(comments_raw)
            user_rows = parse_salesforce_users_csv(users_raw)

            if dry_run:
                data = load_data_fn()
                contacts = data.setdefault("crm_contacts", [])
                token = _preview_token(
                    publications_raw,
                    comments_raw,
                    users_raw,
                    contacts,
                )
                result = import_salesforce_chatter_rows(
                    contacts,
                    publication_rows,
                    comment_rows,
                    user_rows,
                    dry_run=True,
                )
            else:
                with shared_lock:
                    data = load_data_fn()
                    contacts = data.setdefault("crm_contacts", [])
                    token = _preview_token(
                        publications_raw,
                        comments_raw,
                        users_raw,
                        contacts,
                    )
                    if not supplied_token:
                        return jsonify({
                            "error": (
                                "Un aperçu doit être validé avant l'import."
                            )
                        }), 409
                    if supplied_token != token:
                        return jsonify({
                            "error": (
                                "Les fichiers ou le CRM ont changé depuis "
                                "l'aperçu. Relancez l'analyse."
                            )
                        }), 409
                    result = import_salesforce_chatter_rows(
                        contacts,
                        publication_rows,
                        comment_rows,
                        user_rows,
                        dry_run=False,
                    )
                    summary = {
                        "date": dt.datetime.now(_PARIS_TZ).isoformat(),
                        "batch_id": result.get("batch_id"),
                        "publications_filename": publications_file.filename,
                        "comments_filename": comments_file.filename,
                        "users_filename": users_file.filename,
                        **{
                            key: result.get(key, 0)
                            for key in (
                                "publication_csv_rows",
                                "comment_csv_rows",
                                "user_rows",
                                "matched_contacts",
                                "contacts_updated",
                                "publications_created",
                                "publications_updated",
                                "comments_created",
                                "comments_updated",
                                "unmatched_parent_count",
                                "ambiguous_parent_count",
                            )
                        },
                    }
                    data["crm_salesforce_chatter_last_import"] = summary
                    history = data.setdefault(
                        "crm_salesforce_chatter_import_history", []
                    )
                    history.insert(0, summary)
                    del history[20:]
                    save_data_fn(data)

            result.update({
                "preview_token": token,
                "publications_filename": publications_file.filename,
                "comments_filename": comments_file.filename,
                "users_filename": users_file.filename,
            })
            return jsonify(result)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover
            app.logger.exception(
                "Erreur import historique Chatter Salesforce"
            )
            return jsonify({
                "error": f"L'import a échoué : {exc}"
            }), 500
