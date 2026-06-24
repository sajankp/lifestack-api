# Troubleshooting Cloudflare Tunnels

This guide outlines how to detect, diagnose, and resolve issues with duplicate or stale Cloudflare Tunnel connectors that lead to persistent `502 Bad Gateway` errors.

## The Symptoms
- Local API container is healthy and responds to local probes (`curl http://localhost:8000/health` works).
- The `cloudflared` container starts successfully and logs that connections are established.
- Public domain requests (`https://<your-api-domain>/health`) return `502 Bad Gateway` instantly, but **nothing is logged** in either the VM's `cloudflared` container or the `api` container.

This indicates that traffic is being routed to another active connector on the same tunnel token that cannot reach the backend.

---

## 1. Finding Duplicate Connectors via Cloudflare Dashboard
The most reliable way to check for duplicate connectors is through the Cloudflare Zero Trust console:

1. Log in to the [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/).
2. Navigate to **Networks** -> **Tunnels**.
3. Locate the tunnel corresponding to your token (search by name or tunnel ID visible in the Zero Trust dashboard).
4. Click on the tunnel to open its details pane.
5. Under the **Connectors** tab, check the list of active connector sessions:
   - A single running `cloudflared` container normally displays **4 active connections** from the same public IP and host name.
   - If you see multiple hosts, multiple distinct IP addresses, or duplicate sets of connections listed, you have duplicate connectors running.
   - Identify the source host or IP of the duplicate connector to locate where it is running.

---

## 2. Locating Duplicate Instances on Your Local Network / VMs

If you suspect another instance is running, check the following potential sources:

### A. Local Machine/Laptop
Ensure you do not have a local development environment running the same docker-compose stack.
- Check running docker containers:
  ```bash
  docker ps -a | grep cloudflared
  ```
- Check if you have a standalone `cloudflared` daemon running:
  ```bash
  ps -ef | grep cloudflared
  ```

### B. Multiple VM Workspaces
If you are developing in multiple sandboxes or cloned workspaces, check if another workspace container is running in the background.
- Stop docker compose in other workspace folders:
  ```bash
  docker compose down
  ```

---

## 3. Resolving the Issue
Once the duplicate connectors are stopped, wait 1–2 minutes for Cloudflare's Edge Routing table to clear the stale sessions.

If the issue persists:
1. Run a complete down/up cycle on the VM:
   ```bash
   docker compose down && docker compose up -d --build
   ```
2. Monitor the active connector logs on the VM:
   ```bash
   docker compose logs cloudflared -f
   ```
3. Verify the public route resolves:
   ```bash
   curl -I https://<your-api-domain>/health
   ```
