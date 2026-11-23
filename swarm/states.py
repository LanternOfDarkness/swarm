"""Plan and goal state definitions for swarm."""

PLAN_STATES = ["DRAFT", "REVIEW", "APPROVED", "EXECUTING", "VERIFYING", "FIXING",
               "MERGED", "MEASURING", "DONE", "BLOCKED", "DROPPED"]
TERMINAL = {"DONE", "DROPPED"}
TRANSITIONS = {
    "DRAFT": {"REVIEW"},
    "REVIEW": {"DRAFT", "APPROVED"},
    "APPROVED": {"EXECUTING"},
    "EXECUTING": {"VERIFYING"},
    "VERIFYING": {"FIXING", "MERGED"},
    "FIXING": {"VERIFYING"},
    "MERGED": {"MEASURING", "DONE"},
    "MEASURING": {"DONE"},
}
MAX_FIX_ROUNDS = 2
MAX_REVIEW_ROUNDS = 2
MAX_MERGE_ATTEMPTS = 2
ACTIVE_PLAN_STATES = ("EXECUTING", "FIXING", "VERIFYING")
GOAL_STATES = ["QUEUED", "ACTIVE", "BLOCKED", "DONE", "DROPPED"]
RUN_KINDS = ["architect", "reviewer", "implementer", "verifier", "runner", "other"]
RUN_STATUSES = ["RUNNING", "OK", "FAILED", "COST_LIMIT"]


def check_plan_move(current: str, target: str, fix_rounds: int,
                    blocked_from: str | None) -> None:
    """Raise ValueError unless a plan may move from ``current`` to ``target``."""
    if target == "DROPPED" and current not in TERMINAL:
        return
    if target == "BLOCKED" and current not in TERMINAL | {"BLOCKED"}:
        return
    if current == "BLOCKED" and blocked_from is not None and target == blocked_from:
        return
    if target in TRANSITIONS.get(current, set()):
        if current == "VERIFYING" and target == "FIXING" and fix_rounds >= MAX_FIX_ROUNDS:
            raise ValueError(
                "fix round limit (2) reached: move the plan to BLOCKED and ask the operator"
            )
        return

    if target == "BLOCKED" and current == "BLOCKED":
        raise ValueError("plan is already BLOCKED")
    if current == "BLOCKED":
        if blocked_from is None:
            raise ValueError(
                f"plan is BLOCKED with no recorded previous state; cannot move to {target}"
            )
        raise ValueError(
            f"a BLOCKED plan can only return to {blocked_from}, not move to {target}"
        )
    if current in TERMINAL:
        raise ValueError(f"plan is {current}, a terminal state; it cannot move to {target}")
    allowed = ", ".join(sorted(TRANSITIONS.get(current, set())))
    if not allowed:
        raise ValueError(f"plan is in unknown state {current}; cannot move to {target}")
    raise ValueError(f"cannot move a plan from {current} to {target}; allowed: {allowed}")
