"""Fixture RTBF endpoint for the W5 compliance scanner.

Not executed by any tests — only the static source analyser reads this
file to confirm the ``rightToBeForgotten`` sentinel + the GDPR delete
route pattern.
"""

from __future__ import annotations

# gdpr:rtbf
def erase_user_data(user_id: str) -> dict:
    """Fixture handler that marks the user's record for deletion.

    Route: ``DELETE /gdpr/delete/{user_id}``
    """
    return {"user_id": user_id, "status": "scheduled_for_erase"}
