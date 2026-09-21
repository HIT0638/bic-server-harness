"""Structured errors with stable codes (part of the CLI/MCP contract)."""


class BridgeError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        d = {"code": self.code, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d
