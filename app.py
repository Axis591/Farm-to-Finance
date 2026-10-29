import os, time, logging, re
from datetime import date, timedelta
from typing import Optional
import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Farm to Finance Data API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS","*").split(","), allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")
CACHE = {}
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "1800"))
HTTP = requests.Session()
HTTP.headers.update({"User-Agent":"FarmToFinanceDashboard/1.0 (data integration; contact admin)"})

STATES = [
"Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat","Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh","Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab","Rajasthan","Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand","West Bengal",
"Andaman and Nicobar Islands","Chandigarh","Dadra and Nagar Haveli and Daman and Diu","Delhi","Jammu and Kashmir","Ladakh","Lakshadweep","Puducherry"
]
def cached(key, fn):
    now=time.time()
    if key in CACHE and now-CACHE[key][0] < CACHE_TTL:
        return CACHE[key][1]
    value=fn()
    CACHE[key]=(now,value)
    return value

def result(source, status, data=None, updated=None, note=None, url=None):
    return {"source":source,"status":status,"data":data,"updated_at":updated,"note":note,"url":url}

def fy_dates(fy: str):
    # Expected FY 2020-21; archive window is Apr 1 to Mar 31.
    try:
        start=int(fy[:4])
        return f"{start}-04-01", f"{start+1}-03-31"
    except Exception:
        y=date.today().year
        return f"{y}-04-01", f"{y+1}-03-31"

def geocode_district(district: str, state: str):
    """Resolve a district using an exact place query first, then safe state-aware fallbacks."""
    state_clean = state.strip()
    district_clean = district.strip()
    if not district_clean:
        return None

    # Nominatim can resolve administrative district boundaries that the city-focused
    # Open-Meteo geocoder may not return. Use a descriptive User-Agent and limit
    # requests to one query per dashboard cache window in normal use.
    try:
        r = HTTP.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{district_clean}, {state_clean}, India", "format": "jsonv2", "limit": 5, "countrycodes": "in"},
            headers={"Accept-Language": "en"},
            timeout=15,
        )
        r.raise_for_status()
        places = r.json()
        if places:
            # Prefer an administrative boundary result when one is available.
            places.sort(key=lambda x: (
                0 if "boundary" in str(x.get("type", "")).lower() or "administrative" in str(x.get("type", "")).lower() else 1,
                0 if district_clean.lower() in str(x.get("display_name", "")).lower() else 1,
            ))
            p = places[0]
            return {
                "name": district_clean,
                "admin1": state_clean,
                "latitude": float(p["lat"]),
                "longitude": float(p["lon"]),
            }
    except Exception:
        logging.info("Nominatim district lookup unavailable; trying Open-Meteo geocoding")

    # Open-Meteo search fallback. Rank candidates by country, selected state and
    # district-name similarity; never silently use a same-named place in another state.
    queries = [district_clean, f"{district_clean}, {state_clean}, India"]
    candidates = []
    for query in queries:
        try:
            r = HTTP.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": query, "count": 20, "language": "en", "format": "json"},
                timeout=15,
            )
            r.raise_for_status()
            candidates.extend(r.json().get("results", []))
        except Exception:
            continue

    state_norm = state_clean.lower().replace(" ", "").replace("&", "and")
    district_norm = district_clean.lower().replace(" ", "").replace("-", "")
    scored = []
    seen = set()
    for place in candidates:
        if place.get("country_code") != "IN":
            continue
        admins = [str(place.get(k, "")) for k in ("admin1", "admin2", "admin3", "admin4")]
        admin_norm = "".join(x.lower().replace(" ", "").replace("&", "and") for x in admins)
        name_norm = str(place.get("name", "")).lower().replace(" ", "").replace("-", "")
        # State must match when administrative metadata is present. Avoid guessing
        # based only on a district name shared by multiple Indian states.
        state_match = state_norm in admin_norm or state_norm in name_norm
        name_match = name_norm == district_norm or district_norm in name_norm or name_norm in district_norm
        if not state_match or not name_match:
            continue
        identity = (place.get("latitude"), place.get("longitude"))
        if identity in seen:
            continue
        seen.add(identity)
        scored.append((0 if name_norm == district_norm else 1, place))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    return scored[0][1]

def open_meteo_climate(state, district, fy):
    start, fy_end = fy_dates(fy)
    # The archive endpoint accepts historical observations only and recent days
    # may not be published yet. Never send a future fiscal-year end date to it.
    available_end = (date.today() - timedelta(days=5)).isoformat()
    end = min(fy_end, available_end)
    if start > end:
        return result(
            "Open-Meteo historical weather", "unavailable",
            note="This financial year has no published historical weather dates yet. Select a past financial year or try again when observations are available.",
            url="https://open-meteo.com/",
        )
    place=geocode_district(district,state)
    if not place:
        return result("Open-Meteo historical weather","unavailable",note="Could not resolve this district to a reliable coordinate. No district value was inferred.",url="https://open-meteo.com/")
    # Historical daily series; use temperature and precipitation as climate context, not as a formal hazard index.
    url="https://archive-api.open-meteo.com/v1/archive"
    params={"latitude":place["latitude"],"longitude":place["longitude"],"start_date":start,"end_date":end,
            "daily":"precipitation_sum,temperature_2m_max,temperature_2m_min","timezone":"Asia/Kolkata"}
    r=HTTP.get(url,params=params,timeout=35)
    r.raise_for_status()
    j=r.json(); daily=j.get("daily",{})
    rain=[v for v in daily.get("precipitation_sum",[]) if isinstance(v,(int,float))]
    tx=[v for v in daily.get("temperature_2m_max",[]) if isinstance(v,(int,float))]
    tn=[v for v in daily.get("temperature_2m_min",[]) if isinstance(v,(int,float))]
    data={"resolved_location":place.get("name"),"admin1":place.get("admin1"),"latitude":place["latitude"],"longitude":place["longitude"],
          "period_start":start,"period_end":end,"days_with_precipitation_data":len(rain),
          "precipitation_total_mm":round(sum(rain),1) if rain else None,
          "max_temperature_mean_c":round(sum(tx)/len(tx),1) if tx else None,
          "min_temperature_mean_c":round(sum(tn)/len(tn),1) if tn else None}
    return result("Open-Meteo historical weather","available" if rain or tx or tn else "unavailable",data,
                  updated=date.today().isoformat(),note=f"Historical observations available for {start} to {end} only. The selected financial year ends {fy_end}; future dates and the latest unpublished days are excluded. This is a gridded estimate for the geocoded location, not an official district aggregate or a formal climate-risk score.",url="https://open-meteo.com/")

def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

def _records(payload):
    """Extract record arrays from common API response envelopes."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "items", "commodities", "geographies", "states", "districts", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = _records(value)
                if nested:
                    return nested
        if payload and all(isinstance(v, (dict, list)) for v in payload.values()):
            rows = []
            for name, value in payload.items():
                if isinstance(value, dict):
                    row = dict(value); row.setdefault("name", name); rows.append(row)
                elif isinstance(value, list):
                    rows.extend(dict(x, state_name=name) for x in value if isinstance(x, dict))
            return rows
    return []

def _field(row, *keys):
    if not isinstance(row, dict): return None
    normalized = {_norm(k): v for k, v in row.items()}
    for key in keys:
        value = normalized.get(_norm(key))
        if value not in (None, ""): return value
    return None

def _ceda_api(path):
    return "https://api.ceda.ashoka.edu.in/agmarknet/" + path.lstrip("/")

def ceda_geographies():
    def fetch():
        response = HTTP.get(_ceda_api("geographies"), timeout=25)
        response.raise_for_status()
        payload = response.json()
        if not _records(payload): raise ValueError("CEDA geography response had no recognizable rows")
        return payload
    return cached("ceda:geographies", fetch)

def ceda_districts_for_state(state: str):
    """Return CEDA geography suggestions rather than unrelated geocoder place names."""
    try:
        rows = _records(ceda_geographies())
        target = _norm(state); found = set()
        for row in rows:
            state_name = _field(row, "state", "state_name", "stateName", "parent_state", "parentState")
            district_name = _field(row, "district", "district_name", "districtName")
            if state_name and district_name and _norm(state_name) == target:
                found.add(str(district_name).strip())
            row_name = _field(row, "name", "label", "state", "state_name", "stateName")
            if _norm(row_name) == target:
                for child in _records(row):
                    child_name = _field(child, "district", "district_name", "districtName", "name", "label")
                    if child_name and _norm(child_name) != target: found.add(str(child_name).strip())
        return sorted(found, key=str.casefold)
    except Exception:
        logging.exception("CEDA geography lookup failed")
        return []

def ceda_agri_market(state: str, district: str, fy: str):
    """Fetch AGMARKNET mandi prices for the selected geography and financial-year window."""
    source = "CEDA Agri Market Data (AGMARKNET mandi prices)"
    source_url = "https://agmarknet.ceda.ashoka.edu.in/"
    try:
        start, fy_end = fy_dates(fy)
        end = min(fy_end, date.today().isoformat())
        if start > end:
            return result(source, "unavailable", note="This financial year has no published dates yet. Select a past or current financial year.", url=source_url)
        cr = HTTP.get(_ceda_api("commodities"), timeout=25); cr.raise_for_status()
        commodities = _records(cr.json())
        if not commodities:
            return result(source, "unavailable", note="CEDA documented API returned an unrecognized commodity list.", url=source_url)
        commodity = next((x for x in commodities if _norm(_field(x, "name", "commodity", "cmdty", "commodity_name", "label", "commodityName")) == "wheat"), None)
        if commodity is None:
            commodity = next((x for x in commodities if "wheat" in _norm(" ".join(str(v) for v in x.values()))), None)
        if commodity is None:
            return result(source, "unavailable", note="CEDA API is responding, but Wheat was not found in its current commodity list. No substitute commodity or price was assumed.", url=source_url)
        geo_rows = _records(ceda_geographies())
        target_state, target_district = _norm(state), _norm(district)
        state_id = district_id = None
        for row in geo_rows:
            row_state = _field(row, "state", "state_name", "stateName", "parent_state", "parentState")
            row_district = _field(row, "district", "district_name", "districtName")
            if row_state and _norm(row_state) == target_state:
                state_id = state_id or _field(row, "state_id", "stateId", "state_code", "stateCode")
                if row_district and _norm(row_district) == target_district:
                    district_id = _field(row, "district_id", "districtId", "district_code", "districtCode", "id")
            row_name = _field(row, "name", "label", "state", "state_name", "stateName")
            if _norm(row_name) == target_state:
                state_id = state_id or _field(row, "state_id", "stateId", "state_code", "stateCode", "id", "value")
                for child in _records(row):
                    child_name = _field(child, "district", "district_name", "districtName", "name", "label")
                    if _norm(child_name) == target_district:
                        district_id = _field(child, "district_id", "districtId", "district_code", "districtCode", "id", "value")
        commodity_id = _field(commodity, "commodity_id", "commodityId", "id", "value", "commodity_code", "commodityCode")
        if commodity_id is None:
            return result(source, "unavailable", note="CEDA listed Wheat but did not expose a recognized commodity identifier.", url=source_url)
        if state_id is None or district_id is None:
            return result(source, "unavailable", note="CEDA geography data did not provide a confident ID match for " + district + ", " + state + ". No other district was substituted.", data={"requested_state":state,"requested_district":district,"district_suggestions":ceda_districts_for_state(state)[:100]}, url=source_url)
        base = {"commodity_id":commodity_id, "calculation_type":"d", "start_date":start, "end_date":end}
        variants = [dict(base, state_id=state_id, district_id=district_id), dict(base, state=state, district=district), dict(base, state_id=state_id, district=district)]
        price_rows = []; last_error = None
        for body in variants:
            try:
                pr = HTTP.post(_ceda_api("prices"), json=body, timeout=35)
                if pr.status_code in (400, 422): last_error = "HTTP " + str(pr.status_code); continue
                pr.raise_for_status(); price_rows = _records(pr.json())
                if price_rows: break
            except requests.exceptions.RequestException as exc:
                last_error = type(exc).__name__
        normalized = []
        for row in price_rows:
            item = {"commodity":_field(row, "cmdty", "commodity", "commodity_name", "commodityName") or "Wheat",
                    "date":_field(row, "t", "date", "arrival_date", "month", "period"),
                    "minimum_price_rs_per_quintal":_field(row, "p_min", "min_price", "minimum_price", "minPrice"),
                    "maximum_price_rs_per_quintal":_field(row, "p_max", "max_price", "maximum_price", "maxPrice"),
                    "modal_price_rs_per_quintal":_field(row, "p_modal", "modal_price", "price", "modalPrice")}
            if any(item[k] is not None for k in ("minimum_price_rs_per_quintal", "maximum_price_rs_per_quintal", "modal_price_rs_per_quintal")): normalized.append(item)
        normalized.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
        latest = normalized[0] if normalized else None
        if not latest and last_error:
            return result(source, "unavailable", note="CEDA API connected but did not return a usable price series for this district/FY (" + last_error + "). No values were fabricated.", url=source_url)
        return result(source, "available" if latest else "no_matching_records",
                      {"commodity":"Wheat","state":state,"district":district,"financial_year":fy,"latest_available_record":latest,
                       "recent_records":normalized[:12],"price_unit":"INR per quintal","requested_period_start":start,"requested_period_end":end},
                      updated=(latest.get("date") if latest else None),
                      note="CEDA Agri Market Data sourced from AGMARKNET. Default commodity: Wheat. Selected FY window: " + start + " to " + end + ". These are mandi prices, not crop area or production. Data may lag. Credit: Centre for Economic Data & Analysis (CEDA), Ashoka University.", url=source_url)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "unknown"
        logging.exception("CEDA API HTTP error")
        return result(source, "error", note="CEDA documented API returned HTTP " + str(code) + ". Try again later; no substitute values were generated.", url=source_url)
    except requests.exceptions.Timeout:
        logging.exception("CEDA API timed out")
        return result(source, "error", note="CEDA API timed out. Try again later; no substitute values were generated.", url=source_url)
    except (requests.exceptions.RequestException, ValueError, TypeError) as e:
        logging.exception("CEDA API fetch failed")
        return result(source, "error", note="Could not read CEDA API data (" + type(e).__name__ + "). No substitute values were generated.", url=source_url)

def ogd_resource(resource_id: str, state: str, district: str, fy: str, label: str):
    key=os.getenv("DATA_GOV_IN_API_KEY","").strip()
    resource_id=(resource_id or "").strip()
    if not key or not resource_id:
        return result(label,"not_configured",note="Add DATA_GOV_IN_API_KEY and this dataset's real data.gov.in resource ID in Render. No data has been fabricated.",url="https://data.gov.in/")
    # data.gov.in resource IDs are UUIDs (with or without hyphens). API keys pasted
    # into this field are not resource IDs and should not be sent as endpoint paths.
    if not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", resource_id):
        return result(label,"not_configured",note="The value in this resource-ID field is not a valid data.gov.in resource ID format. Replace it with the dataset's UUID from its API details; keep your API key only in DATA_GOV_IN_API_KEY.",url="https://www.data.gov.in/")
    def fetch():
        url=f"https://api.data.gov.in/resource/{resource_id}"
        params={"api-key":key,"format":"json","limit":1000}
        r=HTTP.get(url,params=params,timeout=30); r.raise_for_status()
        j=r.json()
        records=j.get("records",[])
        # Filter records only where fields are recognizably present; do not claim a match without evidence.
        filtered=[]
        for row in records:
            blob=" ".join(str(v).lower() for v in row.values())
            if state.lower() in blob and district.lower() in blob and (fy in blob or fy[:4] in blob or str(int(fy[:4])+1) in blob):
                filtered.append(row)
        return result(label,"available" if filtered else "no_matching_records",{"records":filtered[:100],"records_returned":len(filtered)},
                      updated=date.today().isoformat(),note="Records fetched from OGD API and filtered by text match. Verify field names and reporting-period semantics before using in financial decisions.",url=f"https://api.data.gov.in/resource/{resource_id}")
    try:
        return cached(f"ogd:{resource_id}:{state}:{district}:{fy}",fetch)
    except requests.exceptions.ConnectionError:
        logging.exception("OGD connection failed")
        return result(label,"error",note="Render could not connect to the data.gov.in API. Verify the service has outbound network access and try again later; this is not evidence that the dataset contains zero records.",url="https://data.gov.in/")
    except requests.exceptions.Timeout:
        logging.exception("OGD request timed out")
        return result(label,"error",note="The data.gov.in API request timed out. Try again later or check provider availability and limits.",url="https://data.gov.in/")
    except requests.exceptions.HTTPError as e:
        status_code=e.response.status_code if e.response is not None else "unknown"
        logging.exception("OGD HTTP error")
        note="data.gov.in rejected the request. Check the API key and resource ID."
        if status_code in (401,403):
            note="data.gov.in rejected the API key or access permissions. Verify the key in Render."
        elif status_code == 404:
            note="data.gov.in could not find this resource ID. Copy the resource UUID from the dataset's API details."
        return result(label,"error",note=f"{note} (HTTP {status_code}).",url="https://data.gov.in/")
    except (ValueError, requests.exceptions.RequestException) as e:
        logging.exception("OGD fetch failed")
        return result(label,"error",note=f"Could not read the data.gov.in response ({type(e).__name__}). Verify the resource ID, key, and provider availability.",url="https://data.gov.in/")

@app.get("/")
def home():
    return FileResponse("static/index.html")

@app.get("/api/health")
def health():
    return {"status":"ok","service":"Farm to Finance API","cache_ttl_seconds":CACHE_TTL}

@app.get("/api/states")
def states():
    return {"states":STATES}

@app.get("/api/districts")
def districts(state: str = Query(..., min_length=2)):
    # Use CEDA's own geography register instead of unrelated geocoder place names.
    found = ceda_districts_for_state(state)
    if found:
        return {"districts":found,"source":"CEDA Agri Market Data geography register","note":"Suggestions from CEDA's market-data register; administrative coverage can differ. You may type a district if it is missing."}
    return {"districts":[],"source":"unavailable","note":"CEDA district suggestions are temporarily unavailable; type the district name and refresh."}

@app.get("/api/dashboard")
def dashboard(state: str, district: str, fy: str):
    key=f"dash:{state}:{district}:{fy}"
    def load():
        sources={}
        try:
            sources["climate"]=open_meteo_climate(state,district,fy)
        except Exception as e:
            logging.exception("Climate fetch failed")
            sources["climate"]=result("Open-Meteo historical weather","error",note=f"Provider request failed: {type(e).__name__}.")
        sources["agriculture"]=ceda_agri_market(state,district,fy)
        sources["finance"]=ogd_resource(os.getenv("OGD_FINANCE_RESOURCE_ID",""),state,district,fy,"Public finance / scheme data (OGD)")
        sources["insurance"]=ogd_resource(os.getenv("OGD_INSURANCE_RESOURCE_ID",""),state,district,fy,"Crop insurance / risk finance (OGD)")
        # IMD district warnings are current context, not historical FY data.
        # Try the documented IMD API first, then the public Mausam district-warning
        # endpoint as a fallback. Keep provider errors explicit rather than fabricating data.
        imd_urls = [
            "https://api.imd.gov.in/api/v1/districtwarning",
            "https://mausam.imd.gov.in/api/warnings_district_api.php",
        ]
        imd_errors = []
        imd_loaded = False
        for imd_url in imd_urls:
            try:
                r = HTTP.get(imd_url, timeout=20)
                r.raise_for_status()
                raw = r.json()
                records = raw if isinstance(raw, list) else raw.get("data", raw.get("records", raw.get("warnings", []))) if isinstance(raw, dict) else []
                if isinstance(records, dict):
                    records = list(records.values())
                if not isinstance(records, list):
                    records = []
                matched = []
                for row in records:
                    if not isinstance(row, dict):
                        continue
                    row_district = str(row.get("District", row.get("district", row.get("district_name", ""))))
                    if district.strip().casefold() == row_district.strip().casefold():
                        matched.append(row)
                if matched:
                    sources["imd_current"] = result(
                        "India Meteorological Department (IMD) district warnings", "available",
                        {"records": matched[:10]}, updated=date.today().isoformat(),
                        note="Current IMD district warning feed; not a historical FY dataset. Verify issue date and district identity before operational use.",
                        url=imd_url)
                    imd_loaded = True
                    break
                # A successful but unrecognized payload is not evidence that there are no warnings.
                if records:
                    imd_errors.append("Provider responded but district records could not be matched")
                else:
                    imd_errors.append("Provider returned no recognizable records")
            except Exception as e:
                imd_errors.append(type(e).__name__)
        if not imd_loaded:
            sources["imd_current"] = result(
                "India Meteorological Department (IMD) district warnings", "unavailable",
                note="Could not retrieve and match district warnings from the documented IMD endpoint or the public Mausam fallback. Check provider availability and district spelling; do not interpret this as 'no warning'.",
                url="https://api.imd.gov.in/public/api_reference.html")

        return {"selection":{"state":state,"district":district,"fy":fy},"generated_at":date.today().isoformat(),"sources":sources,
          "disclaimer":"Agriculture uses CEDA documented Agri Market Data API for AGMARKNET mandi prices (Wheat default), filtered to the selected financial-year date window where available; these are not crop-production totals. Public finance and insurance feeds require valid dataset-specific OGD resource UUIDs and remain unavailable until configured. Climate data is a location-based gridded estimate, not a certified district statistic. Do not treat modelled indicators as official financial or climate-risk ratings."}
    return cached(key,load)

@app.get("/api/config")
def config():
    return {"ogd_key_configured":bool(os.getenv("DATA_GOV_IN_API_KEY","").strip()),
            "agriculture_resource_configured":bool(os.getenv("OGD_AGRICULTURE_RESOURCE_ID","").strip()),
            "finance_resource_configured":bool(os.getenv("OGD_FINANCE_RESOURCE_ID","").strip()),
            "insurance_resource_configured":bool(os.getenv("OGD_INSURANCE_RESOURCE_ID","").strip()),
            "message":"Set API credentials and resource IDs in the server environment to activate these connectors."}
