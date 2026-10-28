"""Decide whether a stored case still needs a review.

Intake writes revisions and does not look at the reviewer queue. This decision
runs after that write. It never approves a draft and it never calls intake, so
a poll cannot create another revision of the same documents.
"""

from __future__ import annotations

# A draft in one of these states already has a human review, or the human
# decision has been copied forward. "recorded" is not here: the draft exists
# and still needs POST /v1/reviews.
QUEUED_STATUSES = frozenset({"in_review", "approved", "created", "not_submitted"})


def queue_action(revision, draft, prior_revision) -> str:
    """Return ``submit``, ``process``, or ``skip`` for the latest input revision.

    ``revision`` is the latest input revision. ``draft`` is the latest draft for
    that case, or None. ``prior_revision`` is the input revision the draft was
    built from when that is not ``revision``.
    """
    if revision is None:
        return "skip"
    if draft is not None and draft.input_revision == revision.revision:
        if draft.status == "recorded" and not draft.outcome:
            return "submit"
        return "skip"
    if (
        draft is not None
        and draft.status in QUEUED_STATUSES
        and prior_revision is not None
        and prior_revision.document_set_sha256 == revision.document_set_sha256
    ):
        return "skip"
    return "process"
