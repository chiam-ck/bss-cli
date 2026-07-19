"""Policy base: PolicyViolation exception and @policy decorator."""


class PolicyViolation(Exception):
    def __init__(self, rule: str, message: str, context: dict | None = None):
        self.rule = rule
        self.message = message
        self.context = context or {}
        super().__init__(message)


def policy(rule_id: str):
    """Decorator that tags a policy function with its rule_id."""

    def wrap(fn):
        fn.__policy_rule__ = rule_id
        return fn

    return wrap
