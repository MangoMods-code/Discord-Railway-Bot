# railway_api.py — All Railway GraphQL API calls, clean and centralized.

import logging
from typing import Optional
import aiohttp

logger = logging.getLogger("railway_api")

RAILWAY_GQL = "https://backboard.railway.app/graphql/v2"


class RailwayAPIError(Exception):
    pass


class RailwayClient:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        self.token = token
        self.session = session
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def _query(self, gql: str, variables: Optional[dict] = None) -> dict:
        payload = {"query": gql, "variables": variables or {}}
        try:
            async with self.session.post(
                RAILWAY_GQL,
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    raise RailwayAPIError("Railway token invalid or expired (401)")
                if resp.status == 429:
                    raise RailwayAPIError("Railway API rate limited (429)")
                resp.raise_for_status()
                data = await resp.json()
                if "errors" in data:
                    msgs = "; ".join(e.get("message", "unknown") for e in data["errors"])
                    raise RailwayAPIError(f"GQL errors: {msgs}")
                return data.get("data", {})
        except aiohttp.ClientConnectionError as e:
            raise RailwayAPIError(f"Connection error: {e}") from e
        except aiohttp.ClientResponseError as e:
            raise RailwayAPIError(f"HTTP {e.status}: {e.message}") from e

    # ── PROJECTS ──────────────────────────────────────────────────────────────

    async def get_projects(self) -> list[dict]:
        """Fetch all projects. Workspace-scoped tokens use `projects` root query.
        Personal tokens use `me { projects }`. We try workspace first, fall back to me."""

        # Workspace-scoped token path
        workspace_gql = """
        query {
          projects(first: 100) {
            edges {
              node {
                id
                name
                createdAt
                environments {
                  edges {
                    node {
                      id
                      name
                    }
                  }
                }
                services {
                  edges {
                    node {
                      id
                      name
                    }
                  }
                }
              }
            }
          }
        }
        """
        try:
            data = await self._query(workspace_gql)
            projects = [e["node"] for e in data.get("projects", {}).get("edges", [])]
            if projects:
                logger.info("Fetched %d project(s) via workspace token path", len(projects))
                return projects
        except RailwayAPIError as e:
            logger.debug("Workspace project query failed, trying me path: %s", e)

        # Personal token fallback
        me_gql = """
        query {
          me {
            projects {
              edges {
                node {
                  id
                  name
                  createdAt
                  environments {
                    edges {
                      node {
                        id
                        name
                      }
                    }
                  }
                  services {
                    edges {
                      node {
                        id
                        name
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = await self._query(me_gql)
        projects = [e["node"] for e in data["me"]["projects"]["edges"]]
        logger.info("Fetched %d project(s) via personal token path", len(projects))
        return projects

    # ── DEPLOYMENTS ──────────────────────────────────────────────────────────

    async def get_recent_deployments(
        self, project_id: str, environment_id: str, limit: int = 5
    ) -> list[dict]:
        gql = """
        query($projectId: String!, $environmentId: String!, $limit: Int!) {
          deployments(
            first: $limit
            input: { projectId: $projectId, environmentId: $environmentId }
          ) {
            edges {
              node {
                id
                status
                createdAt
                updatedAt
                canRedeploy
                service {
                  id
                  name
                }
              }
            }
          }
        }
        """
        data = await self._query(
            gql,
            {"projectId": project_id, "environmentId": environment_id, "limit": limit},
        )
        return [e["node"] for e in data.get("deployments", {}).get("edges", [])]

    # ── LOGS ─────────────────────────────────────────────────────────────────

    async def get_deployment_logs(self, deployment_id: str) -> list[dict]:
        gql = """
        query($deploymentId: String!) {
          deploymentLogs(deploymentId: $deploymentId) {
            message
            severity
            timestamp
          }
        }
        """
        try:
            data = await self._query(gql, {"deploymentId": deployment_id})
            return data.get("deploymentLogs") or []
        except RailwayAPIError as e:
            logger.warning("Log fetch failed for %s: %s", deployment_id[:8], e)
            return []

    async def get_build_logs(self, deployment_id: str) -> list[dict]:
        gql = """
        query($deploymentId: String!) {
          buildLogs(deploymentId: $deploymentId) {
            message
            severity
            timestamp
          }
        }
        """
        try:
            data = await self._query(gql, {"deploymentId": deployment_id})
            return data.get("buildLogs") or []
        except RailwayAPIError as e:
            logger.warning("Build log fetch failed for %s: %s", deployment_id[:8], e)
            return []

    # ── METRICS ──────────────────────────────────────────────────────────────

    async def get_service_metrics(self, service_id: str, environment_id: str) -> Optional[dict]:
        gql = """
        query($serviceId: String!, $environmentId: String!) {
          serviceMetrics(serviceId: $serviceId, environmentId: $environmentId) {
            cpuPercentage
            memoryUsageBytes
            memoryLimitBytes
          }
        }
        """
        try:
            data = await self._query(
                gql, {"serviceId": service_id, "environmentId": environment_id}
            )
            return data.get("serviceMetrics")
        except RailwayAPIError:
            return None

    # ── REDEPLOY ─────────────────────────────────────────────────────────────

    async def redeploy(self, deployment_id: str) -> bool:
        gql = """
        mutation($id: String!) {
          deploymentRedeploy(id: $id) {
            id
            status
          }
        }
        """
        try:
            await self._query(gql, {"id": deployment_id})
            return True
        except RailwayAPIError as e:
            logger.error("Redeploy failed for %s: %s", deployment_id[:8], e)
            return False
