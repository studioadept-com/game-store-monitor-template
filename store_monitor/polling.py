"""Persisted scheduling for Google's separately limited release-status endpoint."""

# Lifecycle states that only change after the track configuration changes too
# (sending a draft for review, or fixing a rejection, needs a new release).
SETTLED = {"draft", "published", "rejected"}


def last_attempt(snapshot):
    return snapshot.get("lifecycle_attempted_at") or snapshot.get("lifecycle_checked_at") or 0


def release_key(release):
    codes = release.get("versionCodes", [])
    return str(max(int(code) for code in codes)) if codes else "draft:" + release.get("name", "")


def track_fingerprint(track):
    """What the Edits API says about a track, to spot changes since the last confirmed status."""
    return sorted(f"{release_key(release)}|{release.get('status', '')}|{release.get('userFraction')}"
                  for release in track.get("releases", []))


def awaiting_decision(track, prior):
    if track_fingerprint(track) != prior.get("lifecycle_fingerprint"):
        return True  # New or changed release since the last successful status check.
    records = prior.get("releases", {})
    # Releases the status endpoint omitted were obsolete, so only recorded ones count.
    return any(records[key].get("status") not in SETTLED
               for key in map(release_key, track.get("releases", [])) if key in records)


def lifecycle_order(targets, previous, config, now):
    # Much shorter than the five-minute schedule, so start-time jitter never makes a
    # run inherit its predecessor's requests; it still blocks bursts from manual runs.
    window = max(60, int(config.get("lifecycle_window_seconds", 120)))
    limit = max(1, int(config.get("lifecycle_max_requests", 6)))
    recent = sum(last_attempt(value) > now - window for value in previous.values()
                 if value.get("store") == "google" and value.get("kind") != "builds")
    remaining = max(0, limit - recent)
    active_interval = max(window, int(config.get("lifecycle_active_interval_seconds", 900)))
    idle_interval = max(active_interval, int(config.get("lifecycle_idle_interval_seconds", 21600)))
    candidates = []
    for scope, target in targets.items():
        prior = previous.get(scope, {})
        # Spend the small budget on tracks where Google still owes a review or
        # publication decision; settled tracks only get an occasional recheck.
        active = awaiting_decision(target["track"], prior)
        attempted = last_attempt(prior)
        due = attempted + (active_interval if active else idle_interval) if attempted else 0
        if due <= now:
            # Earlier due dates eventually outrank active tracks, preventing starvation.
            candidates.append((due, 0 if active else 1, scope))
    return [scope for _, _, scope in sorted(candidates)[:remaining]]
