import os, time, logging, re
from datetime import date, datetime, timedelta
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

# CEDA Agri Market Data uses Census-2011 state identifiers. This is a public
# historical price source and does not require a data.gov.in API key.
CEDA_BASE = "https://agmarknet.ceda.ashoka.edu.in/api"
CEDA_STATE_IDS = {
    "Jammu and Kashmir": 1, "Himachal Pradesh": 2, "Punjab": 3, "Chandigarh": 4,
    "Uttarakhand": 5, "Haryana": 6, "Delhi": 7, "Rajasthan": 8, "Uttar Pradesh": 9,
    "Bihar": 10, "Sikkim": 11, "Arunachal Pradesh": 12, "Nagaland": 13,
    "Manipur": 14, "Mizoram": 15, "Tripura": 16, "Meghalaya": 17, "Assam": 18,
    "West Bengal": 19, "Jharkhand": 20, "Odisha": 21, "Chhattisgarh": 22,
    "Madhya Pradesh": 23, "Gujarat": 24, "Daman and Diu": 25,
    "Dadra and Nagar Haveli": 26, "Maharashtra": 27, "Andhra Pradesh": 28,
    "Karnataka": 29, "Goa": 30, "Lakshadweep": 31, "Kerala": 32,
    "Tamil Nadu": 33, "Puducherry": 34, "Andaman and Nicobar Islands": 35,
    "Telangana": 36,
}
CEDA_STATE_ALIASES = {
    "NCT of Delhi": "Delhi", "National Capital Territory of Delhi": "Delhi",
    "Orissa": "Odisha", "Uttaranchal": "Uttarakhand",
    "Dadra and Nagar Haveli and Daman and Diu": "Daman and Diu",
    "Ladakh": "Jammu and Kashmir",
}
CEDA_DISTRICT_ALIASES = {
    "paschim medinipur": {"paschim medinipur", "west midnapore", "midnapore west", "west medinipur"},
    "purba medinipur": {"purba medinipur", "east midnapore", "midnapore east", "east medinipur"},
    "paschim bardhaman": {"paschim bardhaman", "bardhaman west", "west bardhaman"},
    "purba bardhaman": {"purba bardhaman", "bardhaman east", "east bardhaman", "bardhaman"},
    "hooghly": {"hooghly", "hugli"},
    "sri potti sriramulu nellore": {"sri potti sriramulu nellore", "nellore"},
    "y.s.r.": {"y.s.r.", "ysr", "kadapa", "cuddapah"},
}

def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

def _items(payload):
    """Return records from list, object-envelope, dictionary-map, or string-list API responses."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, (dict, str, int, float))]
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "items", "commodities", "states", "districts", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, (dict, str, int, float))]
            if isinstance(value, dict):
                nested = _items(value)
                if nested:
                    return nested
        return [dict(value, name=key) if isinstance(value, dict) else {"name":key,"id":value}
                for key, value in payload.items() if isinstance(value, (dict, str, int, float))]
    return []

def _field(row, *keys):
    if not isinstance(row, dict):
        return None
    normalized = {_norm(key): value for key, value in row.items()}
    for key in keys:
        value = normalized.get(_norm(key))
        if value not in (None, ""):
            return value
    return None

def _ceda_request(path, params=None, body=None, method="GET"):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://agmarknet.ceda.ashoka.edu.in",
        "Referer": "https://agmarknet.ceda.ashoka.edu.in/",
        "User-Agent": "FarmToFinanceDashboard/1.0",
    }
    url = f"{CEDA_BASE}/{path.lstrip('/')}"
    if method == "POST":
        response = HTTP.post(url, params=params or {}, json=body or {}, headers=headers, timeout=35)
    else:
        response = HTTP.get(url, params=params or {}, headers=headers, timeout=25)
    response.raise_for_status()
    return response.json()

def _ceda_state_ids(state):
    selected = CEDA_STATE_ALIASES.get(state, state)
    if selected == "Daman and Diu":
        return [25, 26]
    return [CEDA_STATE_IDS[selected]] if selected in CEDA_STATE_IDS else []

def _row_name(row, keys):
    if isinstance(row, str):
        return row.strip()
    return str(_field(row, *keys) or "").strip()

def ceda_districts_for_state(state: str):
    """Get actual CEDA district names from the selected state's geography records."""
    names = set()
    for state_id in _ceda_state_ids(state):
        try:
            payload = _ceda_request("districts", params={"state_id":state_id})
            rows = _items(payload)
            for row in rows:
                name = _row_name(row, ("district", "district_name", "districtName", "name", "label", "title"))
                if name and _norm(name) not in {_norm("data"), _norm("district")}:
                    names.add(name)
        except Exception:
            logging.exception("CEDA district lookup failed for state ID %s", state_id)
            continue
    return sorted(names, key=str.casefold)

def _ceda_find_district(state, district):
    aliases = CEDA_DISTRICT_ALIASES.get(district.strip().casefold(), {district.strip().casefold()})
    alias_norms = {_norm(name) for name in aliases}
    for state_id in _ceda_state_ids(state):
        try:
            rows = _items(_ceda_request("districts", params={"state_id":state_id}))
            for row in rows:
                name = _row_name(row, ("district", "district_name", "districtName", "name", "label", "title"))
                row_id = _field(row, "district_id", "districtId", "id", "value", "district_code", "districtCode")
                if _norm(name) in alias_norms and row_id is not None:
                    return state_id, row_id, name
        except Exception:
            logging.exception("CEDA district ID lookup failed for state ID %s", state_id)
    return None, None, None

def ceda_agri_market(state: str, district: str, fy: str):
    """Fetch real CEDA/AGMARKNET market prices for one state, district and financial year."""
    source = "CEDA Agri Market Data (AGMARKNET mandi prices)"
    source_url = "https://agmarknet.ceda.ashoka.edu.in/"
    try:
        state_ids = _ceda_state_ids(state)
        if not state_ids:
            return result(source, "unavailable", note=f"CEDA does not have a verified state ID mapping for {state}. No different state was substituted.", url=source_url)
        start, fy_end = fy_dates(fy)
        end = min(fy_end, date.today().isoformat())
        if start > end:
            return result(source, "unavailable", note=f"FY {fy} has not started yet.", url=source_url)
        # CEDA historical series begins in 2014; do not represent older periods as covered.
        if int(start[:4]) < 2014:
            return result(source, "unavailable", note="CEDA historical mandi-price coverage used by this dashboard starts in FY 2014-15. Choose FY 2014-15 or later.", url=source_url)

        state_id, district_id, resolved_district = _ceda_find_district(state, district)
        if district_id is None:
            suggestions = ceda_districts_for_state(state)
            return result(source, "unavailable",
                          note=f"CEDA's district register did not match '{district}' in {state}. Select a listed district or check spelling; no other district was substituted.",
                          data={"requested_state":state, "requested_district":district, "available_districts_sample":suggestions[:80]},
                          url=source_url)

        commodities = _items(cached("ceda:commodities", lambda: _ceda_request("commodities")))
        commodity_row = next((row for row in commodities
                              if _norm(_row_name(row, ("commodity", "commodity_name", "commodityName", "name", "label", "cmdty"))) == "wheat"), None)
        if commodity_row is None:
            commodity_row = next((row for row in commodities
                                  if "wheat" in _norm(str(row))), None)
        if commodity_row is None:
            return result(source, "unavailable", note="CEDA's commodity API did not return a recognizable Wheat entry. No price was invented.", url=source_url)
        commodity_id = commodity_row if isinstance(commodity_row, (int, float)) else _field(
            commodity_row, "commodity_id", "commodityId", "id", "value", "commodity_code", "commodityCode")
        if commodity_id is None:
            return result(source, "unavailable", note="CEDA returned Wheat without a recognizable commodity ID. No price was inferred.", url=source_url)

        # Monthly aggregation keeps the FY response compact and makes past FY selection meaningful.
        body = {
            "state_id": state_id,
            "commodity_id": commodity_id,
            "district_id": district_id,
            "calculation_type": "m",
            "chart_type": "timeseries",
            "start_date": start + "T00:00:00Z",
            "end_date": end + "T23:59:59Z",
        }
        payload = _ceda_request("prices", body=body, method="POST")
        rows = _items(payload.get("data", payload) if isinstance(payload, dict) else payload)
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            date_value = _field(row, "t", "date", "period", "arrival_date")
            if not date_value:
                continue
            try:
                observed = date.fromisoformat(str(date_value)[:10])
            except ValueError:
                try:
                    observed = datetime.strptime(str(date_value)[:10], "%d/%m/%Y").date()
                except ValueError:
                    continue
            if not (date.fromisoformat(start) <= observed <= date.fromisoformat(end)):
                continue
            record = {
                "date": observed.isoformat(),
                "commodity": _field(row, "cmdty", "commodity", "commodity_name") or "Wheat",
                "state": state,
                "district": resolved_district,
                "minimum_price_rs_per_quintal": _field(row, "p_min", "min_price", "minimum_price"),
                "maximum_price_rs_per_quintal": _field(row, "p_max", "max_price", "maximum_price"),
                "modal_price_rs_per_quintal": _field(row, "p_modal", "modal_price", "price"),
            }
            if any(record[k] is not None for k in ("minimum_price_rs_per_quintal", "maximum_price_rs_per_quintal", "modal_price_rs_per_quintal")):
                normalized.append(record)
        normalized.sort(key=lambda item: item["date"], reverse=True)
        latest = normalized[0] if normalized else None
        note = (
            f"CEDA/AGMARKNET monthly mandi-price series for Wheat, {resolved_district}, {state}, FY {fy}. "
            f"Query window: {start} through {end}. This is market-price data, not crop production. "
            "CEDA values may be updated with a lag; the observation date is shown explicitly. "
            "Credit: Centre for Economic Data & Analysis (CEDA), Ashoka University."
        )
        if not latest:
            note = f"CEDA returned no Wheat price records for {resolved_district}, {state}, FY {fy} ({start} to {end}). This is a genuine no-record result for this commodity/geography/period, not zero prices."
        return result(source, "available" if latest else "no_matching_records",
                      {"commodity":"Wheat","state":state,"district":resolved_district,"financial_year":fy,
                       "latest_available_record":latest,"recent_records":normalized[:12],
                       "records_returned":len(normalized),"price_unit":"INR per quintal",
                       "period_start":start,"period_end":end},
                      updated=(latest.get("date") if latest else None), note=note, url=source_url)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "unknown"
        logging.exception("CEDA API HTTP error")
        return result(source, "error", note=f"CEDA market-data API returned HTTP {code}. The provider request failed; no substitute values were generated.", url=source_url)
    except requests.exceptions.Timeout:
        logging.exception("CEDA API timeout")
        return result(source, "error", note="CEDA market-data API timed out. Try again later; no substitute values were generated.", url=source_url)
    except (requests.exceptions.RequestException, ValueError, TypeError, KeyError) as e:
        logging.exception("CEDA API fetch failed")
        return result(source, "error", note=f"Could not read the CEDA API response ({type(e).__name__}). No substitute values were generated.", url=source_url)


def ogd_resource(resource_id: str, state: str, district: str, fy: str, label: str):
    key=os.getenv("DATA_GOV_IN_API_KEY","").strip()
    resource_id=(resource_id or "").strip()
    if not key or not resource_id:
        return result(label,"not_configured",note="Add DATA_GOV_IN_API_KEY and this dataset's real data.gov.in resource ID in Render. No data has been fabricated.",url="https://data.gov.in/")
    # data.gov.in resource IDs are UUIDs (with or without hyphens). API keys pasted
    # into this field are not resource IDs and should not be sent as endpoint paths.
    if not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", resource_id):
        return result(label,"not_configured",note="This panel needs a real dataset resource UUID (not an API key, URL, or placeholder). Copy the UUID from that specific dataset's API details on data.gov.in. Agriculture uses its own built-in AGMARKNET resource.",url="https://www.data.gov.in/")
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
    # Use AGMARKNET's own state/district lookup rather than geocoding place names.
    found = ceda_districts_for_state(state)
    if found:
        return {"districts":found,"source":"AGMARKNET state/district API","note":"Districts supplied by AGMARKNET. If a district is absent, you can type it manually."}
    return {"districts":[],"source":"unavailable","note":"AGMARKNET district lookup is unavailable for this state right now; type the district name manually."}

def official_finance_reference(state: str, district: str, fy: str):
    return result("PM-KISAN official scheme reference", "reference_available",
        {"programme":"Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
         "published_benefit":"Rs 6,000 per year for an eligible farmer family, subject to scheme rules",
         "selection":{"state":state,"district":district,"financial_year":fy},
         "data_level":"National scheme reference only",
         "district_disbursement_data":"Not provided by this reference; no district payment amount is inferred"},
        note="Official scheme information only, not a live district-wise disbursement feed. District payment amounts require an API-enabled government dataset.",
        url="https://pmkisan.gov.in/")

def official_insurance_reference(state: str, district: str, fy: str):
    return result("Pradhan Mantri Fasal Bima Yojana (PMFBY) official reference", "reference_available",
        {"programme":"Pradhan Mantri Fasal Bima Yojana (PMFBY)",
         "purpose":"Crop insurance under notified crops, seasons, areas and policy conditions",
         "selection":{"state":state,"district":district,"financial_year":fy},
         "data_level":"Scheme reference only",
         "district_insured_area_premium_and_claims":"Not supplied by this reference; no district values are inferred"},
        note="Official crop-insurance scheme reference only, not a district-level claims or premiums API. District statistics require an API-enabled dataset.",
        url="https://pmfby.gov.in/")

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
        finance = ogd_resource(os.getenv("OGD_FINANCE_RESOURCE_ID",""),state,district,fy,"Public finance / scheme data (OGD)")
        sources["finance"] = finance if finance.get("status") == "available" else official_finance_reference(state,district,fy)
        insurance = ogd_resource(os.getenv("OGD_INSURANCE_RESOURCE_ID",""),state,district,fy,"Crop insurance / risk finance (OGD)")
        sources["insurance"] = insurance if insurance.get("status") == "available" else official_insurance_reference(state,district,fy)
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
    uuid_pattern = r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    return {"ogd_key_configured":bool(os.getenv("DATA_GOV_IN_API_KEY","").strip()),
            "agriculture_resource_configured":True,
            "finance_resource_configured":bool(re.fullmatch(uuid_pattern, os.getenv("OGD_FINANCE_RESOURCE_ID","").strip())),
            "insurance_resource_configured":bool(re.fullmatch(uuid_pattern, os.getenv("OGD_INSURANCE_RESOURCE_ID","").strip())),
            "message":"Agriculture uses the built-in Government AGMARKNET resource. Finance and insurance require their own real data.gov.in resource UUIDs plus DATA_GOV_IN_API_KEY."}
