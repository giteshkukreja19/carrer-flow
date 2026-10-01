"""Legacy Haveloc automation boundary. Disabled in the local read-only phase."""

class HavelocClient:
    def __init__(self, *args, **kwargs):
        self.base_url = kwargs.get("base_url")
        self.username = kwargs.get("username")
        self.password = kwargs.get("password")

    async def scan(self):
        return {"status": "paused", "message": "Haveloc scanning is disabled in the local read-only phase."}
