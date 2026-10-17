import copy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


APPLE_STATES = {
    "PREPARE_FOR_SUBMISSION": "draft", "READY_FOR_REVIEW": "ready for review",
    "WAITING_FOR_REVIEW": "waiting for review", "IN_REVIEW": "in review",
    "ACCEPTED": "accepted", "PENDING_DEVELOPER_RELEASE": "approved, awaiting your release",
    "PENDING_APPLE_RELEASE": "awaiting Apple's release", "REJECTED": "rejected",
    "METADATA_REJECTED": "metadata rejected", "DEVELOPER_REJECTED": "withdrawn from review",
    "INVALID_BINARY": "invalid build", "PROCESSING_FOR_DISTRIBUTION": "processing for distribution",
    "PROCESSING_FOR_APP_STORE": "processing for distribution",
    "WAITING_FOR_EXPORT_COMPLIANCE": "waiting for export compliance",
    "READY_FOR_DISTRIBUTION": "published", "READY_FOR_SALE": "published",
    "REPLACED_WITH_NEW_VERSION": "replaced by a newer version",
    "REMOVED_FROM_SALE": "removed from sale", "DEVELOPER_REMOVED_FROM_SALE": "removed from sale",
}
GOOGLE_STATES = {
    "DRAFT": "draft", "NOT_SENT_FOR_REVIEW": "ready to submit for review",
    "IN_REVIEW": "in review", "APPROVED_NOT_PUBLISHED": "approved, awaiting your release",
    "NOT_APPROVED": "rejected", "PUBLISHED": "published",
}


def percent(value):
    return f"{value:g}%"


def local_time(iso, display_timezone):
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(display_timezone)).strftime("%Y-%m-%d %H:%M %Z")


def notes_context(record):
    notes = record.get("notes", "").strip()
    if not notes:
        return "\nChange notes: not provided"
    # Full notes are retained in event history; keep the Telegram message compact.
    preview = notes if len(notes) <= 1500 else notes[:1499] + "…"
    locale = f" ({record['notes_locale']})" if record.get("notes_locale") else ""
    return f"\n{record.get('notes_source', 'Release notes')}{locale}:\n{preview}"


def record_prefix(record):
    prefix = record["app_name"]
    if record.get("version") and record["version"] != "unknown":
        prefix += f", version {record['version']}"
    if record.get("build_number"):
        prefix += f" (build {record['build_number']})"
    return prefix


# Event kinds let each destination choose what it receives; see delivery.wants.
BUILD_EVENTS = {"PROCESSING": ("build_processing", "Build uploaded — processing"),
                "VALID": ("build_processed", "Build completed processing"),
                "FAILED": ("build_failed", "Build processing failed"),
                "INVALID": ("build_invalid", "Build processing invalid"),
                "DETECTED": ("build_detected", "New Android build detected")}
STATUS_KINDS = {
    "draft": "draft", "ready for review": "ready_for_review", "waiting for review": "waiting_for_review",
    "in review": "in_review", "accepted": "approved", "approved, awaiting your release": "approved",
    "awaiting Apple's release": "approved", "rejected": "rejected", "metadata rejected": "rejected",
    "invalid build": "rejected", "withdrawn from review": "review_withdrawn", "published": "published",
    "ready to submit for review": "not_sent_for_review",
    "track configured; publication not yet confirmed": "publication_unconfirmed",
}
# In-between states that usually move on within minutes. Their events wait up to
# batch_window_seconds, so the next step can arrive in the same message.
HOLD_STATUSES = {("apple", "ready for review"), ("apple", "accepted"), ("apple", "awaiting Apple's release"),
                 ("apple", "processing for distribution"), ("google", "approved, awaiting your release"),
                 ("google", "track configured; publication not yet confirmed")}


def change(kind, title, notes=False, **extra):
    return {"kind": kind, "title": title, "notes": notes, **extra}


def build_changes(old, new):
    if old is None or old["status"] != new["status"]:
        kind, phrase = BUILD_EVENTS.get(new["status"], ("build_status", "Build status: " + new["status"]))
        return [change(kind, phrase, notes=True)]
    if old.get("notes", "") != new.get("notes", ""):
        return [change("build_notes_updated", "Build notes updated", notes=True)]
    return []


def pending_rollout_change(old, new):
    if new["store"] != "google" or new["status"] == "published":
        return None
    phase = new.get("track_phase")
    rollout = new.get("configured_rollout")
    if phase not in ("inProgress", "halted", "completed"):
        return None
    # Quietly backfill metadata for previously unknown/unchanged releases, while
    # still catching a pre-upgrade draft that has now acquired a rollout setting.
    if old and "track_phase" not in old and old["status"] != "draft":
        return None
    old_phase = old.get("track_phase", "draft" if old.get("status") == "draft" else None) if old else None
    if old and phase == old_phase and rollout == old.get("configured_rollout"):
        return None
    if phase == "halted":
        phrase = "Rollout pause configured"
    elif rollout is not None:
        phrase = f"Rollout set to {percent(rollout)}"
    else:
        phrase = "Rollout configured"
    return phrase + " — publication confirmation pending"


def detect(old, new):
    """Only compare fields that describe observable release changes."""
    if new.get("kind") == "build":
        return build_changes(old, new)
    found = []
    status = new["status"]
    pending_rollout = pending_rollout_change(old, new)
    if (old is None or old["status"] != status) and not (pending_rollout and "publication not yet confirmed" in status):
        if status == "draft":
            phrase = "New version draft"
        elif status == "published":
            phrase = "Moved to production" if new["track"].endswith("production") or new["store"] == "apple" else f"Published to {new['track']}"
        else:
            phrase = status[0].upper() + status[1:]
        found.append(change(STATUS_KINDS.get(status, "status_changed"), phrase, notes="notes" in new))
    if pending_rollout:
        found.append(change("rollout_pending_confirmation", pending_rollout, notes=True, rollout=new.get("configured_rollout")))
    if old and old.get("version") != new["version"]:
        found.append(change("release_renamed", f"Release renamed (was {old['version']})"))
    if old and "build_number" in old and old.get("build_number") != new.get("build_number"):
        phrase = "Release build changed" if new.get("build_number") else "Release build detached"
        found.append(change("release_build_changed", phrase))
    if old and "notes" in old and old.get("notes") != new.get("notes") and old["status"] == status:
        found.append(change("notes_updated", "Release notes updated", notes=True))
    phase = new.get("phase")
    if phase in ("PAUSED", "halted") and (not old or old.get("phase") != phase):
        found.append(change("rollout_paused", "Rollout paused"))
    elif old and old.get("phase") in ("PAUSED", "halted") and phase in ("ACTIVE", "inProgress", "COMPLETE", "completed"):
        found.append(change("rollout_resumed", "Rollout resumed"))
    rollout = new.get("rollout")
    previous = old.get("rollout") if old else None
    if rollout is not None and rollout != previous:
        direction = "increased" if previous is not None and rollout > previous else "decreased" if previous is not None and rollout < previous else "at"
        word = "to" if direction != "at" else ""
        phrase = f"Rollout {direction} {word} {percent(rollout)}".replace("  ", " ")
        # A first percentage arrives together with the publication it belongs to.
        kind = "rollout_started" if direction == "at" else "rollout_completed" if rollout >= 100 else "rollout_changed"
        found.append(change(kind, phrase))
    return found


def describe(record, found):
    """Plain-text message: the history record, dry-run output and delivery fallback."""
    store = "App Store" if record["store"] == "apple" else "Google Play"
    text = f"{record_prefix(record)} — {found['title']}\n{store} · {record['track']}"
    return text + notes_context(record) if found["notes"] else text


def changes(old, new):
    return [describe(new, found) for found in detect(old, new)]


def release_event(record, steps):
    """Structured fields for one or more steps of the same release, rendered by each destination."""
    title = " → ".join(step["title"] for step in steps)
    notes = any(step["notes"] for step in steps)
    rollout = next((step["rollout"] for step in reversed(steps) if "rollout" in step), record.get("rollout"))
    event = {"kind": steps[-1]["kind"], "kinds": [step["kind"] for step in steps], "steps": steps, "title": title,
             "store": record["store"], "app_id": record.get("app_id"), "app_name": record["app_name"]}
    optional = {"bundle_id": record.get("bundle_id"), "track": record.get("track"),
                "version": record.get("version") if record.get("version") != "unknown" else None,
                "build": record.get("build_number"), "rollout": rollout}
    event.update({key: value for key, value in optional.items() if value not in (None, "")})
    if notes:
        event.update(notes=record.get("notes", "").strip(), notes_source=record.get("notes_source", "Release notes"),
                     notes_locale=record.get("notes_locale", ""))
    event["text"] = describe(record, {"title": title, "notes": notes})
    return event


def transitional(record):
    if record.get("kind") == "build":
        return record["store"] == "apple" and record["status"] == "PROCESSING"
    return (record["store"], record["status"]) in HOLD_STATUSES


def held(event, now):
    return "hold_until" in event and datetime.fromisoformat(event["hold_until"]) > now


def monitor_event(kind, title, detail):
    return {"kind": kind, "title": title, "detail": detail, "text": f"{title}: {detail}"}


def empty_state():
    return {"schema": 1, "initialized": [], "scopes": {}, "health": {}, "outbox": [], "sequence": 0,
            "builds_initialized": [], "history": []}


def route(event, destinations):
    """Destinations that want at least one step, and the steps those that want only some may show."""
    steps = event.get("steps") or [{"kind": event["kind"]}]
    pending, visible = [], {}
    for name, wants in (destinations or {"telegram": lambda _: True}).items():
        allowed = [step["kind"] for step in steps if wants({**event, "kind": step["kind"]})]
        if allowed:
            pending.append(name)
            if len(allowed) < len(steps):
                visible[name] = allowed  # A milestone channel never sees the draft or build step folded into it.
    return pending, visible


def enqueue(state, event, record=None, observed_at=None, display_timezone="UTC", destinations=None, history=True):
    """Queue one event for every destination that wants it; release events also go to history."""
    state["sequence"] += 1
    event = {"id": state["sequence"], **event}
    if record:
        when = observed_at or datetime.now(timezone.utc)
        event["observed_at"] = when.isoformat()
        event["text"] += "\nObserved: " + local_time(event["observed_at"], display_timezone)
        if history:
            state.setdefault("history", []).append({**copy.deepcopy(event), "record": copy.deepcopy(record)})
    # Each destination acknowledges separately, so one failing never blocks or repeats another.
    pending, visible = route(event, destinations)
    if pending:
        state["outbox"].append({**event, "pending": pending, **({"visible": visible} if visible else {})})


def queue_release(state, release_id, record, steps, observed_at, display_timezone, destinations, hold_seconds):
    """One event per release per run, folded into an event still held for the same release."""
    previous = next((event for event in state["outbox"] if event.get("release") == release_id and held(event, observed_at)), None)
    if previous:
        state["outbox"].remove(previous)  # No destination has it yet, so it is rebuilt with the new steps.
    event = {**release_event(record, (previous["steps"] if previous else []) + steps), "release": release_id}
    if hold_seconds and transitional(record):
        event["hold_until"] = (observed_at + timedelta(seconds=hold_seconds)).isoformat()
    enqueue(state, event, record, observed_at, display_timezone, destinations, history=False)
    # History records what this run observed, not the combined message.
    entry = {**release_event(record, steps), "id": state["sequence"], "observed_at": observed_at.isoformat()}
    entry["text"] += "\nObserved: " + local_time(entry["observed_at"], display_timezone)
    state.setdefault("history", []).append({**entry, "record": copy.deepcopy(record)})


def reconcile(state, snapshots, errors, successes, threshold=3, observed_at=None, display_timezone="UTC",
              destinations=None, hold_seconds=0):
    """Preserve failed scopes, establish quiet baselines, queue changes durably."""
    observed_at = observed_at or datetime.now(timezone.utc)
    result = copy.deepcopy(state)
    if "google/lifecycle_api" in errors:
        # Consolidate the old per-track quota alerts into one service-level alert.
        for scope, health in list(result["health"].items()):
            if scope.startswith("google/") and "Listing releases quota exceeded" in health.get("error", ""):
                del result["health"][scope]
    initialized = set(state["initialized"])
    builds_initialized = set(state.get("builds_initialized", []))
    result.setdefault("builds_initialized", [])
    result.setdefault("history", [])
    initial_counts = {}
    for scope, snapshot in snapshots.items():
        store = snapshot["store"]
        build_snapshot = snapshot.get("kind") == "builds"
        old = state["scopes"].get(scope, {}).get("releases", {})
        recovering_without_baseline = scope not in state["scopes"] and any(
            scope == failed or scope.startswith(failed + "/") for failed in state["health"]
        )
        records = dict(old)
        for key, release in snapshot["releases"].items():
            first_fallback_observation = (
                snapshot.get("lifecycle_degraded") and not state["scopes"].get(scope, {}).get("track_baselined") and key not in old
            )
            component_ready = store in builds_initialized if build_snapshot else store in initialized
            if (component_ready and not recovering_without_baseline and not first_fallback_observation
                    and not state["scopes"].get(scope, {}).get("baseline_pending")):
                if steps := detect(old.get(key), release):
                    # Several steps seen in one run become one message.
                    queue_release(result, f"{scope}/{key}", release, steps, observed_at, display_timezone,
                                  destinations, hold_seconds)
            records[key] = release
        # Keep absent releases in history. API omission is not proof of deletion.
        result["scopes"][scope] = {**snapshot, "releases": records}
        if build_snapshot and store not in result["builds_initialized"]:
            result["builds_initialized"].append(store)
        if not build_snapshot and store not in initialized:
            initial_counts.setdefault(store, set()).add(snapshot["app_id"])
    for store, app_ids in initial_counts.items():
        result["initialized"].append(store)
        enqueue(result, monitor_event("monitor_connected", "Monitoring connected",
                                      f"{len(app_ids)} {'App Store' if store == 'apple' else 'Google Play'} app(s). Current releases recorded; future changes will be reported."),
                destinations=destinations)
    for scope in successes:
        previous = result["health"].pop(scope, None)
        if previous and previous.get("alerted"):
            enqueue(result, monitor_event("monitor_recovered", "Monitor recovered", scope), destinations=destinations)
    for scope, error in errors.items():
        entry = result["health"].setdefault(scope, {"count": 0, "alerted": False})
        entry["count"] = min(entry["count"] + 1, threshold)
        entry["error"] = error
        if entry["count"] >= threshold and not entry["alerted"]:
            enqueue(result, monitor_event("monitor_attention", "Monitor needs attention",
                                          f"{scope}\n{error}. Checks have failed {threshold} times; previous release state is preserved."),
                    destinations=destinations)
            entry["alerted"] = True
    return result
