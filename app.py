import os, time, logging, re
from datetime import date, datetime, timedelta
from typing import Optional
from io import BytesIO
import xml.etree.ElementTree as ET
import requests
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pypdf import PdfReader

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
    district_key = district.strip().casefold()
    aliases = set(CEDA_DISTRICT_ALIASES.get(district_key, {district_key}))
    for canonical, variants in CEDA_DISTRICT_ALIASES.items():
        if district_key in variants:
            aliases.add(canonical)
            aliases.update(variants)
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
        district_level = district_id is not None
        if not district_level:
            # CEDA accepts district_id=0 for state-level aggregation. This is explicitly
            # labelled as state-level data, never presented as a district observation.
            state_id = state_ids[0]
            district_id = 0
            resolved_district = f"{state} state aggregate (district '{district}' not matched)"

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
            (f"CEDA/AGMARKNET monthly mandi-price series for Wheat, {resolved_district}, FY {fy}. "
             if district_level else f"CEDA/AGMARKNET state-level aggregate for {state}; district '{district}' could not be mapped confidently, so these records are NOT district-specific. FY {fy}. ")
            + f"Query window: {start} through {end}. This is market-price data, not crop production. "
            "CEDA values may be updated with a lag; the observation date is shown explicitly. "
            "Credit: Centre for Economic Data & Analysis (CEDA), Ashoka University."
        )
        if not latest:
            note = f"CEDA returned no Wheat price records for {resolved_district}, {state}, FY {fy} ({start} to {end}). This is a genuine no-record result for this commodity/geography/period, not zero prices."
        return result(source, "available" if latest else "no_matching_records",
                      {"commodity":"Wheat","state":state,"district":(resolved_district if district_level else district),
                       "data_level":("district" if district_level else "state aggregate fallback"),
                       "financial_year":fy,"latest_available_record":latest,"recent_records":normalized[:12],
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
    return {"status":"ok","service":"Farm to Finance API","cache_ttl_seconds":CACHE_TTL,
            "revision":os.getenv("RENDER_GIT_COMMIT","unknown"),
            "agriculture_source":"CEDA Agri Market Data (AGMARKNET)",
            "agriculture_history_from_fy":"2014-15"}

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


# Source data from Ministry of Finance, Government of India, Lok Sabha Starred
# Question 430, answered 23 March 2026. Annexure-II: FY 2022-23 to FY 2024-25
# state/UT-wise agricultural-credit disbursement. Annexure-I: provisional
# state/UT-by-bank-group outstanding as of 31 December 2025, in Rs crore.
AGRI_CREDIT_SOURCE = "https://sansad.in/getFile/loksabhaquestions/annex/187/AS430_6PvNZy.pdf?source=pqals"
AGRI_CREDIT_DISBURSEMENT = {
    "A and N Island": [402, 437, 476], "Andhra Pradesh": [239188, 300497, 327037],
    "Arunachal Pradesh": [637, 950, 1213], "Assam": [9062, 11564, 14514],
    "Bihar": [52994, 70321, 66492], "Chandigarh": [1635, 1938, 2287],
    "Chhattisgarh": [29266, 33838, 37154], "D & N Haveli and Daman & Diu": [342, 393, 383],
    "Delhi": [19350, 24157, 29317], "Goa": [1905, 2429, 14492],
    "Gujarat": [120529, 141161, 163965], "Haryana": [81805, 89396, 99222],
    "Himachal Pradesh": [11003, 11628, 14426], "Jammu and Kashmir": [13152, 12624, 17841],
    "Jharkhand": [12171, 22109, 16553], "Karnataka": [172470, 207615, 231295],
    "Kerala": [140236, 160468, 177510], "Ladakh": [334, 352, 420],
    "Lakshadweep": [23, 62, 40], "Madhya Pradesh": [100076, 116397, 133121],
    "Maharashtra": [150532, 168547, 201867], "Manipur": [645, 649, 842],
    "Meghalaya": [404, 478, 554], "Mizoram": [309, 427, 1715],
    "Nagaland": [340, 412, 553], "Odisha": [47749, 60365, 80673],
    "Puducherry": [5574, 7066, 8183], "Punjab": [90798, 95687, 100544],
    "Rajasthan": [141612, 153593, 168545], "Sikkim": [340, 405, 3016],
    "Tamil Nadu": [375747, 452384, 483678], "Telangana": [109798, 139703, 164908],
    "Tripura": [1670, 2175, 2971], "Uttar Pradesh": [151452, 171390, 196225],
    "Uttarakhand": [11244, 12674, 15779], "West Bengal": [60369, 74343, 89070],
}
# Each tuple is scheduled commercial banks, regional rural banks, rural cooperative
# banks. None is an em-dash in the official table (not reported), not a zero.
AGRI_CREDIT_OUTSTANDING = {
    "A and N Island": (438, None, 21), "Andhra Pradesh": (285380, 55013, 34861),
    "Arunachal Pradesh": (518, None, None), "Assam": (16744, 5172, 226),
    "Bihar": (58468, 24425, 7427), "Chandigarh": (2808, None, 0),
    "Chhattisgarh": (24511, 3598, 5934), "D & N Haveli and Daman & Diu": (259, None, 19),
    "Delhi": (29472, None, 0), "Goa": (1641, None, 224), "Gujarat": (110877, 13345, 44239),
    "Haryana": (71483, 11967, 15054), "Himachal Pradesh": (9415, 1929, 3404),
    "Jammu and Kashmir": (13330, 1472, 22), "Jharkhand": (17618, 2949, 60),
    "Karnataka": (180145, None, 30100), "Kerala": (127460, 21795, 11768),
    "Ladakh": (467, 12, 15), "Lakshadweep": (53, None, None),
    "Madhya Pradesh": (129428, 12141, 29189), "Maharashtra": (249900, 10488, 46906),
    "Manipur": (981, 130, 181), "Meghalaya": (643, 463, 185),
    "Mizoram": (229, 898, 511), "Nagaland": (609, 18, 235),
    "Odisha": (49027, 8338, 22332), "Puducherry": (6623, 1648, 6),
    "Punjab": (83057, 8790, 8475), "Rajasthan": (144192, 30069, 18032),
    "Sikkim": (360, None, 41), "Tamil Nadu": (456718, 26997, 22576),
    "Telangana": (131703, 26541, 17717), "Tripura": (1825, 1079, 1858),
    "Uttar Pradesh": (169112, 60985, None), "Uttarakhand": (13045, 771, 2611),
    "West Bengal": (73365, 10851, 6791),
}

def _credit_state_key(state):
    aliases = {
        "aandamanandnicobarislands": "A and N Island",
        "andamanandnicobarislands": "A and N Island",
        "dadraandnagarhavelianddamananddiu": "D & N Haveli and Daman & Diu",
        "dadraandnagarhaveli": "D & N Haveli and Daman & Diu",
        "damananddiu": "D & N Haveli and Daman & Diu",
        "puducherry": "Puducherry",
        "jammuandkashmir": "Jammu and Kashmir",
    }
    normalized = _norm(state)
    if normalized in aliases:
        return aliases[normalized]
    return next((name for name in AGRI_CREDIT_DISBURSEMENT if _norm(name) == normalized), None)

def official_agriculture_credit(state: str, district: str, fy: str):
    key = _credit_state_key(state)
    if key is None:
        return result("Agriculture credit disbursement/outstanding (Government of India / NABARD)", "unavailable",
                      note="The selected state/UT does not match the official credit table. No amount has been inferred.", url=AGRI_CREDIT_SOURCE)
    years = ["2022-23", "2023-24", "2024-25"]
    values = AGRI_CREDIT_DISBURSEMENT[key]
    history = [{"financial_year": y, "amount_crore": v} for y, v in zip(years, values)]
    top = sorted([{"state": name, "amount_crore": vals[-1]} for name, vals in AGRI_CREDIT_DISBURSEMENT.items()],
                 key=lambda x: x["amount_crore"], reverse=True)[:10]
    outstanding = AGRI_CREDIT_OUTSTANDING.get(key, (None, None, None))
    groups = [
        {"bank_group": "Scheduled commercial banks", "amount_crore": outstanding[0]},
        {"bank_group": "Regional rural banks", "amount_crore": outstanding[1]},
        {"bank_group": "Rural cooperative banks", "amount_crore": outstanding[2]},
    ]
    reported = [x["amount_crore"] for x in groups if x["amount_crore"] is not None]
    data = {
        "state": state, "selected_district": district,
        "data_level": "State/UT aggregate; not district-specific",
        "disbursement_history": history, "top_states_disbursement": top,
        "outstanding_by_bank_group": [x for x in groups if x["amount_crore"] is not None],
        "outstanding_reported_sum_crore": sum(reported) if reported else None,
        "outstanding_total_is_complete": len(reported) == 3,
        "outstanding_as_of": "2025-12-31",
        "unit": "INR crore",
        "district_level_credit": None,
    }
    return result("Agriculture credit disbursement/outstanding (Government of India / NABARD)", "available",
                  data, updated="2026-03-23",
                  note="Official Ministry of Finance answer to Lok Sabha Starred Question 430, 23 March 2026; Annexure-II gives state/UT-wise disbursement for FY 2022-23 to 2024-25. Annexure-I reports provisional outstanding balances by bank group as of 31 December 2025, in ₹ crore. Source: banks' ENSURE submissions to NABARD. Not district-level. Em dashes mean not reported, not zero. Disbursement minus outstanding is not loan recovery.",
                  url=AGRI_CREDIT_SOURCE)

AGRI_INCOME_PDF = "https://issrf.in/wp-content/uploads/2025/04/District-Level-Estimates-of-Agriculture-Household-Income.pdf"
AGRI_INCOME_SOURCE = "https://issrf.in/2025/04/15/district-level-estimates-of-agriculture-household-income-2019-in-india/"
INCOME_STATE_NAMES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Jammu and Kashmir", "Ladakh", "Delhi", "Andaman and Nicobar Islands",
    "Chandigarh", "Dadra and Nagar Haveli", "Daman and Diu", "Lakshadweep", "Puducherry"
]
INCOME_STATE_ALIASES = {
    "jammuandkashmir": "Jammu and Kashmir",
    "andamanandnicobarislands": "Andaman and Nicobar Islands",
    "dadraandnagarhavelianddamananddiu": "Dadra and Nagar Haveli",
    "uttarakhand": "Uttarakhand", "odisha": "Odisha",
}
INCOME_DISTRICT_ALIASES = {
    "paschimmedinipur": ["Paschim Medinipur", "Pashchim Medinipur", "West Midnapore", "Midnapore West"],
    "pashchimmedinipur": ["Paschim Medinipur", "Pashchim Medinipur", "West Midnapore", "Midnapore West"],
    "purba medinipur": ["Purba Medinipur", "East Midnapore", "Midnapore East"],
    "gurugram": ["Gurugram", "Gurgaon"],
    "ahmadabad": ["Ahmedabad", "Ahmadabad"],
    "allahabad": ["Allahabad", "Prayagraj"],
    "prayagraj": ["Allahabad", "Prayagraj"],
    "nellore": ["Nellore", "Sri Potti Sriramulu Nellore"],
    "ysr": ["YSR", "Y.S.R.", "Cuddapah", "Kadapa"],
}

def _extract_income_rows_from_pdf():
    """Read district income appendix; records remain tagged as model estimates, not profit."""
    try:
        response = HTTP.get(AGRI_INCOME_PDF, timeout=75)
        response.raise_for_status()
        reader = PdfReader(BytesIO(response.content))
    except Exception:
        logging.exception("Could not download/read ISSRF income PDF")
        return []
    num = r"([\d,]+\.\d{2})"
    row_re = re.compile(r"(?<!\d)(\d{1,3})\s+([A-Za-z][A-Za-z&().,/'\- ]{0,75}?)\s+" +
                        r"\s+".join([num] * 7))
    rows = []
    current_state = None
    table_started = False
    states_long = sorted(INCOME_STATE_NAMES, key=len, reverse=True)
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if "Average Monthly Income of Agriculture Households" in page_text or "District Wise Survey" in page_text:
            table_started = True
        # Every appendix state page has its state name in the header, but some tables
        # wrap across a page; retain last state on continuation page.
        head = " ".join(page_text.splitlines()[:20])
        for state_name in states_long:
            if re.search(r"(?i)(?<![A-Za-z])" + re.escape(state_name) + r"(?![A-Za-z])", head):
                if "Annexure-1" not in head or "Average Monthly Income" in page_text:
                    current_state = state_name
                    break
        if not table_started or not current_state:
            continue
        flat = re.sub(r"\s+", " ", page_text)
        for match in row_re.finditer(flat):
            district_name = re.sub(r"\s+", " ", match.group(2)).strip(" .-")
            if not district_name:
                continue
            nums = []
            for raw in match.groups()[2:]:
                try: nums.append(float(raw.replace(",", "")))
                except ValueError: nums.append(None)
            if len(nums) != 7:
                continue
            rows.append({
                "state": current_state, "district": district_name,
                "survey_income_rs": nums[0], "linear_mixed_income_rs": nums[1],
                "spatial_income_rs": nums[2], "survey_rse": nums[3],
                "linear_mixed_rse": nums[4], "spatial_rse": nums[5],
                "monthly_income_rs": nums[6],
                "reference_period": "July 2018–June 2019",
            })
        # Allow next-page continuation only while page header remains same state.
        # The first line of the table often breaks district names, so no strict line parsing.
    dedup, seen = [], set()
    for row in rows:
        key = (_norm(row["state"]), _norm(row["district"]))
        if key not in seen:
            seen.add(key); dedup.append(row)
    logging.info("Parsed %s district income rows from ISSRF PDF", len(dedup))
    return dedup

def _income_rows():
    return cached("issrf_agri_household_income_2019", _extract_income_rows_from_pdf)

def agricultural_household_income(state: str, district: str, fy: str):
    try:
        rows = _income_rows()
        state_target = INCOME_STATE_ALIASES.get(_norm(state), state)
        state_rows = [r for r in rows if _norm(r["state"]) == _norm(state_target)]
        aliases = [district]
        district_key = _norm(district)
        for alias_key, alias_values in INCOME_DISTRICT_ALIASES.items():
            all_names = [alias_key] + alias_values
            if any(_norm(name) == district_key for name in all_names):
                aliases.extend(all_names)
        target_names = {_norm(name) for name in aliases}
        selected = next((r for r in state_rows if _norm(r["district"]) in target_names), None)
        if not selected:
            candidate = [r for r in state_rows if _norm(r["district"]).startswith(_norm(district)) or _norm(district).startswith(_norm(r["district"]))]
            if len(candidate) == 1 and len(_norm(district)) >= 5:
                selected = candidate[0]
        top = sorted(state_rows, key=lambda r: r["monthly_income_rs"], reverse=True)[:10]
        if not rows:
            return result("ISSRF district-level agricultural household income estimates (2018-19)", "unavailable",
                          note="The ISSRF district income report could not be parsed by the service; no income or profit has been invented.", url=AGRI_INCOME_SOURCE)
        if not selected:
            return result("ISSRF district-level agricultural household income estimates (2018-19)", "no_matching_records",
                          {"state":state,"district":district,"top_districts":[{"district":r["district"],"monthly_income_rs":r["monthly_income_rs"]} for r in top]},
                          note="This report has no confidently matched model estimate for this district. Estimates are for July 2018–June 2019 and are not current farm profit.", url=AGRI_INCOME_SOURCE)
        data = {
            "state": state, "district": selected["district"],
            "monthly_income_rs": selected["monthly_income_rs"],
            "annualised_income_rs": round(selected["monthly_income_rs"] * 12, 2),
            "estimate_type": "Benchmark/final model-based agricultural household income",
            "reference_period": "July 2018–June 2019",
            "model_relative_standard_error_percent": selected["linear_mixed_rse"],
            "selected_estimate": {"district":selected["district"],"monthly_income_rs":selected["monthly_income_rs"],
                                   "annualised_income_rs":round(selected["monthly_income_rs"]*12,2),
                                   "reference_period":"July 2018–June 2019"},
            "top_districts": [{"district":r["district"],"monthly_income_rs":r["monthly_income_rs"]} for r in top],
            "state_district_estimate_count": len(state_rows),
        }
        return result("ISSRF district-level agricultural household income estimates (2018-19)", "available",
                      data, updated="2025-04-15",
                      note="The benchmark estimates use NSSO Situation Assessment Survey 2019 data for July 2018–June 2019 combined with small-area linear mixed/spatial models and auxiliary data. The estimate combines cultivation, livestock, wages and non-farm business, so it is not farm-business revenue, crop profit or net income. Some fully urban districts are explicitly unavailable.",
                      url=AGRI_INCOME_SOURCE)
    except Exception as e:
        logging.exception("Income report parse failed")
        return result("ISSRF district-level agricultural household income estimates (2018-19)", "error",
                      note=f"Could not parse the public income report ({type(e).__name__}).", url=AGRI_INCOME_SOURCE)

def official_loan_recovery_gap(state: str, district: str, fy: str):
    return result("Agricultural loan recovery / repayment", "not_available",
                  {"state":state,"district":district,"financial_year":fy,"recovered_amount_crore":None,"recovery_rate_percent":None},
                  note="The linked Lok Sabha/NABARD table reports credit disbursement flows and outstanding loan balances, not repayments collected. Recovery cannot be calculated by subtracting one from the other. A separate district-wise recovery dataset is required.",
                  url=AGRI_CREDIT_SOURCE)


IMD_DISTRICT_RSS = "https://mausam.imd.gov.in/imd_latest/contents/dist_nowcast_rss.php"
IMD_DISTRICT_MAP = "https://mausam.imd.gov.in/responsive/districtWiseWarningGIS.php"

def _imd_item_text(item, tag):
    for child in list(item):
        if child.tag.split("}")[-1].casefold() == tag.casefold():
            return (child.text or "").strip()
    return ""

def imd_public_warnings(state: str, district: str):
    """Use the public IMD district nowcast RSS; fall back to clearly linked official warning map."""
    source = "India Meteorological Department (IMD) district nowcast/warnings"
    # IMD's documented API has a district ID parameter and may deny unregistered clients.
    # The public RSS avoids inventing an Obj_id. Only return a district-specific warning
    # when the district can be matched in the item text.
    try:
        response = HTTP.get(IMD_DISTRICT_RSS, timeout=25, headers={"Accept":"application/rss+xml, application/xml, text/xml"})
        response.raise_for_status()
        payload = response.content
        root = ET.fromstring(payload)
        items = [el for el in root.iter() if el.tag.split("}")[-1].casefold() == "item"]
        parsed = []
        district_norm = _norm(district)
        aliases = {
            "paschimmedinipur": ["paschim medinipur","pashchim medinipur","west midnapore"],
            "purba medinipur": ["purba medinipur","east midnapore"],
            "ahmadabad": ["ahmedabad","ahmadabad"],
            "gurugram": ["gurugram","gurgaon"],
            "prayagraj": ["prayagraj","allahabad"],
            "sri potti sriramulu nellore": ["nellore","sri potti sriramulu nellore"],
            "ysr": ["ysr","kadapa","cuddapah"],
        }
        wanted = {_norm(district)}
        for key, vals in aliases.items():
            if _norm(key) == district_norm or any(_norm(v) == district_norm for v in vals):
                wanted.update(_norm(v) for v in vals)
                wanted.add(_norm(key))
        for item in items:
            title = _imd_item_text(item, "title")
            description = _imd_item_text(item, "description")
            pub_date = _imd_item_text(item, "pubDate")
            link = _imd_item_text(item, "link")
            guid = _imd_item_text(item, "guid")
            body = " ".join(x for x in (title, description, pub_date) if x)
            if body:
                parsed.append({"title":title, "description":description, "published":pub_date, "link":link or guid or IMD_DISTRICT_MAP})
        matches = [row for row in parsed if any(alias in _norm(row["title"] + " " + row["description"]) for alias in wanted if len(alias) >= 5)]
        if matches:
            return result(source, "available",
                          {"state":state,"district":district,"feed":"IMD district nowcast RSS","records":matches[:10],
                           "feed_items_checked":len(parsed),"district_match":True},
                          updated=date.today().isoformat(),
                          note="District-specific text was matched in IMD's public district nowcast RSS feed. This is current nowcast/warning information, not a historical FY series. Confirm issue time and validity before acting.",
                          url=IMD_DISTRICT_RSS)
        # Successful public feed but no text match is NOT interpreted as no warning.
        return result(source, "reference_available",
                      {"state":state,"district":district,"feed":"IMD public district nowcast RSS",
                       "feed_items_checked":len(parsed),"latest_feed_items":parsed[:8],
                       "district_match":False,"official_district_warning_map":IMD_DISTRICT_MAP},
                      updated=date.today().isoformat(),
                      note="IMD's public RSS loaded, but its item text did not confidently identify this selected district. This is not evidence that there is no warning. Use the official district-wise warning map for the live colour-coded district forecast.",
                      url=IMD_DISTRICT_MAP)
    except Exception as e:
        logging.exception("IMD public RSS unavailable")
        return result(source, "reference_available",
                      {"state":state,"district":district,"official_district_warning_map":IMD_DISTRICT_MAP,
                       "official_nowcast_rss":IMD_DISTRICT_RSS},
                      note=f"The public RSS could not be read by this service ({type(e).__name__}); IMD's official live warning map remains linked. The protected JSON API requires a valid district ID and may require IMD access authorization. Do not interpret this as 'no warning'.",
                      url=IMD_DISTRICT_MAP)

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
        sources["agri_credit"]=official_agriculture_credit(state,district,fy)
        sources["farm_income"]=agricultural_household_income(state,district,fy)
        sources["loan_recovery"]=official_loan_recovery_gap(state,district,fy)
        finance = ogd_resource(os.getenv("OGD_FINANCE_RESOURCE_ID",""),state,district,fy,"Public finance / scheme data (OGD)")
        sources["finance"] = finance if finance.get("status") == "available" else official_finance_reference(state,district,fy)
        insurance = ogd_resource(os.getenv("OGD_INSURANCE_RESOURCE_ID",""),state,district,fy,"Crop insurance / risk finance (OGD)")
        sources["insurance"] = insurance if insurance.get("status") == "available" else official_insurance_reference(state,district,fy)
        # Current public IMD district warning/nowcast source; never infer no warning from a missing match.
        sources["imd_current"] = imd_public_warnings(state, district)

        return {"selection":{"state":state,"district":district,"fy":fy},"generated_at":date.today().isoformat(),"sources":sources,
          "disclaimer":"Credit disbursement is state/UT-level from a Ministry of Finance Lok Sabha answer dated 23 March 2026, not district-specific. Outstanding loans are provisional as of 31 December 2025. Agricultural-household income is a model-based estimate for July 2018–June 2019, not farm profit or current revenue. Loan repayments and farm-business profit are not inferred where verified data are unavailable. CEDA/AGMARKNET reports mandi prices, not crop production. Climate uses gridded weather estimates."}
    return cached(key,load)

@app.get("/api/config")
def config():
    uuid_pattern = r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    return {"ogd_key_configured":bool(os.getenv("DATA_GOV_IN_API_KEY","").strip()),
            "agriculture_resource_configured":True,
            "finance_resource_configured":bool(re.fullmatch(uuid_pattern, os.getenv("OGD_FINANCE_RESOURCE_ID","").strip())),
            "insurance_resource_configured":bool(re.fullmatch(uuid_pattern, os.getenv("OGD_INSURANCE_RESOURCE_ID","").strip())),
            "message":"Agriculture uses the built-in Government AGMARKNET resource. Finance and insurance require their own real data.gov.in resource UUIDs plus DATA_GOV_IN_API_KEY."}
