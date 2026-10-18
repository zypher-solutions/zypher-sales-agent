"""When to stop sending automatically. Pure rules, so they can be tested exhaustively.

Bounces and complaints are how a sending domain gets damaged, and on the company's main domain that also
damages normal business email. Pausing is cheap; recovering reputation takes weeks. Only a human resumes.
"""

BOUNCE_RATE_LIMIT = 0.02
RATE_WINDOW = 100  # the rate rule needs at least this many sends before a percentage means anything


def evaluate_stoploss(sent: int, hard_bounces: int, complaint: bool) -> str | None:
    """`sent` and `hard_bounces` count the most recent sends (up to 100). Returns why to pause, or None."""
    if complaint:
        return "a spam complaint was received"
    if sent < RATE_WINDOW and hard_bounces >= 2:
        return f"{hard_bounces} hard bounces in the last {sent} sends"
    if sent >= RATE_WINDOW and hard_bounces / sent > BOUNCE_RATE_LIMIT:
        return f"bounce rate {hard_bounces / sent:.1%} is above 2% over the last {sent} sends"
    return None
