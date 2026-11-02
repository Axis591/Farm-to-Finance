## Public hosting (Google Cloud Run)

For an alternative public HTTPS host with automatic GitHub deployments, follow [DEPLOY_CLOUD_RUN.md](DEPLOY_CLOUD_RUN.md). Cloud Run has a monthly free tier, but requires a linked billing account and may charge if usage exceeds the allowance.

# Farm to Finance — hosted dashboard starter

This is a deployable web application starter, not a deployed public website. It contains a browser dashboard and a FastAPI server that loads data in the background whenever state, district, or FY changes.

## Included now
- State/UT selector and FY selector (FY 2020-21 through current FY).
- District suggestions via Open-Meteo geocoding plus free text entry. Suggestions are **not** an authoritative Local Government Directory (LGD) register.
- Historical daily precipitation and temperature context from Open-Meteo Archive API for the geocoded location and selected FY.
- Current IMD district-warning feed attempt, clearly labelled as current data rather than historical FY data.
- OGD/data.gov.in API connector slots for agriculture, finance, and crop insurance/risk transfer.
- Source status, links, notes, timestamps, and explicit unavailable/not-configured states.
- Backend cache, container deployment, health endpoint.

## Important limitation
A reliable all-India production service needs the real data.gov.in resource IDs for each selected dataset, an API key, a maintained official district master (LGD codes), and source-specific field mappings. The OGD connector slots deliberately show `not_configured` until these are set. They do not invent agriculture or financial figures. The generic OGD text filter is only a starter and should be replaced with dataset-specific filters after resource schemas are reviewed. Some historical weather sources are gridded estimates, not official district statistics. The current IMD warnings feed may be restricted, may change, and is not historical data.

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload
```
Open http://127.0.0.1:8000

## Deploy online (Render example)
1. Put this project in a private Git repository.
2. Create a new Render web service from the repository; `render.yaml` defines a Docker service.
3. In service Environment settings, set `DATA_GOV_IN_API_KEY` and the verified resource IDs for the agriculture, finance and insurance datasets.
4. Deploy. Verify `/api/health` and test multiple state/district/FY combinations.
5. Replace `CORS_ORIGINS=*` with the exact hosted domain before production. Add rate limits, monitoring, authentication if needed, and persistent cache/database if the service grows.

## Source discovery
- OGD Platform: https://data.gov.in/
- OGD APIs: https://data.gov.in/apis
- IMD API reference: https://api.imd.gov.in/public/api_reference.html
- IMD data service portal (historical observations access can require registration): https://dsp.imdpune.gov.in/
- Open-Meteo: https://open-meteo.com/

## Production checklist
- Establish rights and API terms for each source, including private providers.
- Use official LGD district identifiers, not name matching, for joins.
- Map each resource's exact field names, units, release dates and reporting periods.
- Store raw payloads and normalized records with retrieval timestamps and provenance.
- Add retries, timeouts, circuit breakers, request budgets, secrets management and scheduled refreshes.
- Build documented formulas for crop loss, risk scores, savings and investment gap; label modelled outputs separately from observed data.
- Add tests for FY boundaries, renamed districts, missing data, duplicates and stale source responses.
- Configure privacy, access controls, backups, logging, monitoring and rate limiting before multiple-user public launch.
