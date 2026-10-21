"""A small read-only Linear API client for complete, current issue snapshots."""
import aiohttp


class LinearError(Exception):
    def __init__(self, message, *, permanent=False):
        super().__init__(message)
        self.permanent = permanent


class Linear:
    def __init__(self, session, api_key):
        self.session = session
        self.api_key = api_key

    async def query(self, query, variables=None):
        try:
            async with self.session.post(
                "https://api.linear.app/graphql",
                headers={"Authorization": self.api_key},
                json={"query": query, "variables": variables or {}},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    raise LinearError(f"Linear HTTP {response.status}", permanent=response.status in (400, 401, 403))
                result = await response.json()
        except (aiohttp.ClientError, TimeoutError) as error:
            raise LinearError("Linear is temporarily unreachable") from error
        if result.get("errors"):
            # Do not log raw server responses, credentials, or ticket content.
            codes = {item.get("extensions", {}).get("code") for item in result["errors"]}
            raise LinearError("Linear rejected the GraphQL request", permanent=not bool(codes & {"RATELIMITED", "INTERNAL_SERVER_ERROR"}))
        return result["data"]

    async def viewer(self):
        return (await self.query("query { viewer { id name displayName } }"))["viewer"]

    async def issue(self, issue_id):
        result = await self.query("""query ReceiptIssue($id: String!) {
            issue(id: $id) {
                id identifier title description url priority dueDate updatedAt archivedAt
                assignee { id name } state { name } project { name }
                labels { nodes { name } }
            }
        }""", {"id": issue_id})
        return result.get("issue")
