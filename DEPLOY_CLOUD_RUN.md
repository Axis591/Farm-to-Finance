# Deploy Farm to Finance on Google Cloud Run (free-tier option)

This repository already contains a Dockerfile and serves both the FastAPI API and the dashboard from one service. Cloud Run is the recommended alternative to the current Render deployment.

## Important cost and availability notes

- Cloud Run has a monthly request-based free allowance, but it is not an unlimited free hosting promise. Current request-based free tier: 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests per month. Allowances are shared across projects on the billing account and reset monthly.
- Google requires a valid Cloud Billing account even when usage is within the Free Tier. Usage over the free allowance, and other billable products such as Cloud Translation, can create charges. Set a billing budget and alerts before deploying.
- Configure minimum instances to 0 to avoid paying for an always-warm idle instance. The public URL remains reachable; the first request after an idle period can take longer while Cloud Run starts an instance.
- A public Cloud Run URL is a normal HTTPS website URL, not a ChatGPT share link.

Official pricing: https://cloud.google.com/run/pricing
Cloud Run continuous deployment guide: https://cloud.google.com/run/docs/continuous-deployment
Google Cloud Free Tier and billing: https://cloud.google.com/free

## One-time setup (GitHub-connected, automatic future deployments)

1. Open https://console.cloud.google.com/ and sign in to your Google account.
2. Create a Google Cloud project (or select an existing project) and link a billing account. Create a budget alert, for example at ₹100 and ₹500, before enabling services.
3. Enable **Cloud Run**, **Cloud Build** and **Artifact Registry** APIs when prompted.
4. Open https://console.cloud.google.com/run and choose **Create service** → **Continuously deploy from a repository**.
5. Choose **Set up with Cloud Build**, connect GitHub, and grant the Cloud Build GitHub App access to this repository: `Axis591/Farm-to-Finance`.
6. Choose branch `main`.
7. Build configuration:
   - Build type: **Dockerfile**
   - Dockerfile location: `Dockerfile`
   - Build context: repository root (`/`)
8. Configure the Cloud Run service:
   - Service name: `farm-to-finance`
   - Region: `asia-south1` (Mumbai)
   - Authentication: **Allow unauthenticated invocations** (so anyone with the link can visit)
   - Container port: **8000** (the Dockerfile listens on port 8000)
   - Billing: **Request-based**
   - Minimum instances: **0**
   - Maximum instances: **3** initially
   - CPU: **1 vCPU**
   - Memory: **1 GiB**
   - Request timeout: **300 seconds**
   - Concurrency: **20**
9. Add these ordinary environment variables:
   - `CACHE_TTL_SECONDS` = `1800`
   - `CACHE_MAX_ITEMS` = `1500`
   - `CORS_ORIGINS` = `*`
10. Optional private API keys should be added using Google Secret Manager and attached to the service as environment-variable secrets. Do not put keys in GitHub files, source code, or public messages:
    - `DATA_GOV_IN_API_KEY` — enables the daily data.gov.in / AGMARKNET mandi feed.
    - `GOOGLE_TRANSLATE_API_KEY` — enables full-page language translation.
11. Click **Create**. Wait for the first build and deployment to complete. Cloud Run then shows a stable public HTTPS URL ending in `.run.app`.
12. Test these endpoints (replace the sample host with the URL shown by Cloud Run):
    - Website: `https://YOUR-SERVICE-URL.run.app/`
    - API health: `https://YOUR-SERVICE-URL.run.app/api/health`
    - API configuration: `https://YOUR-SERVICE-URL.run.app/api/config`

## Automatic deployments after setup

Because the service is connected to GitHub through Cloud Build, every new commit pushed to the selected `main` branch can trigger a new build and deployment. The service URL stays the same when a new revision is deployed.

## Retiring the previous Render service

Do not delete the existing Render service until the Cloud Run URL passes the website and API health checks. Once Cloud Run is working and you have copied its URL, downgrade or delete the old paid Render service if you no longer need it, to avoid paying for two hosts.

## Why this repository uses the Dockerfile

The application is a FastAPI backend plus an HTML dashboard and an original SVG background asset. A static-only GitHub Pages deployment would not run the `/api/*` endpoints, so it would break data loading, language translation and the dashboard's interactive features. Cloud Run runs the complete Docker application.
