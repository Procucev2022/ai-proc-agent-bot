# Cloudflare Integration & Deployment Guide

This guide explains how to deploy and configure the **AI Procurement Agent** (`ai-proc-agent-bot`) behind **Cloudflare**, either via **Cloudflare DNS Proxy (CDN/WAF)** or via **Cloudflare Zero Trust Tunnel (`cloudflared`)**.

---

## 1. Overview & Architecture

When deploying the bot behind Cloudflare, client traffic (including WhatsApp webhooks, user queries, and operator dashboard sessions) terminates TLS at Cloudflare's edge before being routed to the application:

```
[WhatsApp / Users / Admins]
             │
             ▼ (HTTPS)
   [Cloudflare Edge Network]  (DDoS, WAF, SSL Termination)
             │
      ┌──────┴──────────────────────────┐
      │ (Option A: Proxy)               │ (Option B: Zero Trust Tunnel)
      ▼                                 ▼
[Nginx / Direct Ingress]         [cloudflared container]
      │                                 │
      └──────────────┬──────────────────┘
                     ▼
      [FastAPI (procucev_main_app)]
        ├── Resolves real IP via CF-Connecting-IP
        ├── Logs CF-Ray trace ID
        └── Rate limits per user, not per proxy
```

### Key Built-in Features for Cloudflare:
1. **Real Client IP Resolution**: The app automatically extracts the caller's true IP from `CF-Connecting-IP` or `True-Client-IP`, preventing Cloudflare's proxy IPs from masking client identity.
2. **Distributed Rate Limiting**: SlowAPI keys user requests by their real IP (`CF-Connecting-IP`), preventing all users from sharing a single IP limit.
3. **CF-Ray Observability**: Incoming requests log the Cloudflare `CF-Ray` transaction identifier for end-to-end tracing with Cloudflare logs.
4. **HTTPS Detection**: Detects TLS origin via `CF-Visitor` and `X-Forwarded-Proto` for secure cookie flags.
5. **Reverse Proxy Header Support**: Gunicorn and Uvicorn trust forwarded headers across Docker bridge networks (`FORWARDED_ALLOW_IPS=*`).

---

## 2. Deployment Options

### Option A: Cloudflare Zero Trust Tunnel (`cloudflared`) — Recommended

Cloudflare Tunnel creates an encrypted outbound-only connection between your server and Cloudflare. **No inbound ports (80 or 443) need to be opened on your VM or cloud firewall.**

#### Steps:
1. **Create Tunnel in Cloudflare Zero Trust**:
   - Go to [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/) > **Networks** > **Tunnels**.
   - Click **Create a tunnel**, select **Cloudflared**, and name it (e.g. `procucev-agent-prod`).
   - Copy the provided **Tunnel Token** (`eyJhIjoi...`).

2. **Configure Public Hostname**:
   - In the Tunnel configuration, add a Public Hostname:
     - Subdomain: `agent` (or your chosen subdomain)
     - Domain: `procucev.com`
     - Service Type: `HTTP`
     - URL: `app:8005` (the internal Docker network service name and port)

3. **Configure Environment in `.env`**:
   ```env
   CLOUDFLARE_ENABLED=true
   CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi...your_token_here...
   FORWARDED_ALLOW_IPS=*
   ```

4. **Start the Stack with the Cloudflare Profile**:
   ```bash
   docker compose --profile cloudflare up -d
   ```
   This starts `procucev_main_app`, `procucev_redis`, `procucev_celery_worker`, `procucev_celery_beat`, and `procucev_cloudflared`.

---

### Option B: Cloudflare DNS Proxy (Orange Cloud)

If running a traditional VM where Nginx or Gunicorn binds directly to a public IP:

1. **DNS Settings**:
   - Set an `A` or `CNAME` record in Cloudflare DNS for your domain (e.g. `agent.procucev.com`).
   - Ensure the Proxy status is **Proxied** (Orange Cloud).

2. **SSL/TLS Encryption Mode**:
   - In Cloudflare SSL/TLS settings, set mode to **Full (Strict)** (if using valid certs on origin) or **Full**.

3. **Configure Environment in `.env`**:
   ```env
   CLOUDFLARE_ENABLED=true
   ALLOWED_HOSTS=localhost,127.0.0.1,agent.procucev.com
   FORWARDED_ALLOW_IPS=*
   ```

---

## 3. WhatsApp Webhook Configuration with Cloudflare

1. **Webhook URL**:
   Set your callback URL in Meta App Dashboard / SendMsg portal:
   ```
   https://agent.procucev.com/webhook/whatsapp
   ```

2. **Verification Token**:
   Ensure `WHATSAPP_VERIFY_TOKEN` matches your configured token.

3. **Cloudflare WAF / Security Rules for Webhooks**:
   To prevent Meta / WhatsApp webhook callbacks from being challenged by Cloudflare Bot Fight Mode or Managed Rules:
   - Go to **Security** > **WAF** > **Custom Rules**.
   - Create rule: `Skip WAF for WhatsApp Webhooks`
   - Expression:
     ```
     http.request.uri.path eq "/webhook/whatsapp"
     ```
   - Action: **Skip** (Select: *WAF Managed Rules*, *Bot Management*, *Rate Limiting*).

---

## 4. Recommended Cloudflare Cache Rules

The AI Procurement Agent serves dynamic real-time traffic. Configure Cloudflare to never cache API or webhook traffic:

- In Cloudflare Dashboard > **Caching** > **Cache Rules**:
  - Rule 1: **Bypass Cache for Dynamic Endpoints**
    - Expression: `(http.request.uri.path starts_with "/webhook") or (http.request.uri.path starts_with "/chat") or (http.request.uri.path starts_with "/dashboard") or (http.request.uri.path eq "/health")`
    - Cache eligibility: **Bypass cache**

---

## 5. Testing & Verification

1. **Verify Health Endpoint through Cloudflare**:
   ```bash
   curl -I https://agent.procucev.com/health
   ```
   Expected response:
   ```http
   HTTP/2 200
   cf-ray: ...
   content-type: application/json
   ```

2. **Check Request Logs for Real IP & CF-Ray**:
   Inspect `logs/app/app.log`:
   ```
   [HTTP-IN] GET /health from 103.xxx.xxx.xxx [CF-Ray: 8f4b23190a98-BOM]
   [HTTP-OUT] GET /health -> 200 (1.2ms)
   ```

3. **Check Cloudflare Header Unit Tests**:
   ```powershell
   python scripts/quality_check.py --fast
   ```
