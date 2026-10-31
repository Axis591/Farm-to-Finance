import os, time, logging, re
from concurrent.futures import ThreadPoolExecutor, as_completed
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

def ceda_agri_market(state: str, district: str, fy: str, commodity: str = "Wheat"):
    """Fetch real CEDA/AGMARKNET market prices for one state, district and financial year."""
    commodity_name = (commodity or "Wheat").strip()
    commodity_aliases = {
        "wheat": ["wheat"],
        "rice": ["rice", "paddy", "dhan"],
        "ricepaddy": ["rice", "paddy", "dhan"],
        "maize": ["maize", "corn", "makka"],
        "cotton": ["cotton", "kapas"],
        "onion": ["onion"],
        "potato": ["potato"],
        "tomato": ["tomato"],
        "soybean": ["soybean", "soyabean", "soya bean"],
        "groundnut": ["groundnut", "peanut"],
        "mustard": ["mustard", "rapeseed"],
    }
    aliases = commodity_aliases.get(_norm(commodity_name))
    if not aliases:
        return result("CEDA Agri Market Data (AGMARKNET mandi prices)", "unavailable",
                      note="This commodity is not enabled in the dashboard's verified commodity list. No price was substituted.", url="https://agmarknet.ceda.ashoka.edu.in/")
    source = f"CEDA Agri Market Data (AGMARKNET mandi prices: {commodity_name})"
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
            return result(source, "no_matching_records",
                          {"state":state,"district":district,"data_level":"district lookup unavailable",
                           "financial_year":fy,"records_returned":0,"price_unit":"INR per quintal"},
                          note=f"CEDA could not confidently map {district} within {state}. A state total is not substituted for a district price.",
                          url=source_url)

        commodities = _items(cached("ceda:commodities", lambda: _ceda_request("commodities")))
        alias_norms = {_norm(x) for x in aliases}
        commodity_row = next((row for row in commodities
                              if _norm(_row_name(row, ("commodity", "commodity_name", "commodityName", "name", "label", "cmdty"))) in alias_norms), None)
        if commodity_row is None:
            commodity_row = next((row for row in commodities if _norm(str(row)) in alias_norms), None)
        if commodity_row is None:
            return result(source, "unavailable", note=f"CEDA's commodity API did not return a recognizable {commodity_name} entry. No price was invented.", url=source_url)
        commodity_id = commodity_row if isinstance(commodity_row, (int, float)) else _field(
            commodity_row, "commodity_id", "commodityId", "id", "value", "commodity_code", "commodityCode")
        if commodity_id is None:
            return result(source, "unavailable", note="CEDA returned the selected commodity without a recognizable commodity ID. No price was inferred.", url=source_url)

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
                "commodity": _field(row, "cmdty", "commodity", "commodity_name") or commodity_name,
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
        def _price_float(value):
            try:
                return float(str(value).replace(",", "").strip()) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None
        modal_series = [(row, _price_float(row.get("modal_price_rs_per_quintal"))) for row in normalized[:12]]
        modal_series = [(row, value) for row, value in modal_series if value is not None and value > 0]
        price_change_percent = None
        price_change_start_date = price_change_end_date = None
        price_change_start_value = price_change_end_value = None
        if len(modal_series) >= 2:
            newest_row, newest_price = modal_series[0]
            oldest_row, oldest_price = modal_series[-1]
            price_change_percent = round((newest_price - oldest_price) / oldest_price * 100, 2)
            price_change_start_date, price_change_start_value = oldest_row["date"], oldest_price
            price_change_end_date, price_change_end_value = newest_row["date"], newest_price
        note = (
            (f"CEDA/AGMARKNET monthly mandi-price series for {commodity_name}, {resolved_district}, FY {fy}. "
             if district_level else f"CEDA/AGMARKNET state-level aggregate for {state}; district '{district}' could not be mapped confidently, so these records are NOT district-specific. FY {fy}. ")
            + f"Query window: {start} through {end}. This is market-price data, not crop production. "
            "CEDA values may be updated with a lag; the observation date is shown explicitly. "
            "Credit: Centre for Economic Data & Analysis (CEDA), Ashoka University."
        )
        if not latest:
            note = f"CEDA returned no {commodity_name} price records for {resolved_district}, {state}, FY {fy} ({start} to {end}). This is a genuine no-record result for this commodity/geography/period, not zero prices."
        return result(source, "available" if latest else "no_matching_records",
                      {"commodity":commodity_name,"state":state,"district":(resolved_district if district_level else district),
                       "data_level":("district" if district_level else "state aggregate fallback"),
                       "financial_year":fy,"latest_available_record":latest,"recent_records":normalized[:12],
                       "records_returned":len(normalized),"price_unit":"INR per quintal",
                       "price_change_percent_latest_vs_oldest":price_change_percent,
                       "price_change_start_date":price_change_start_date,
                       "price_change_end_date":price_change_end_date,
                       "price_change_start_value":price_change_start_value,
                       "price_change_end_value":price_change_end_value,
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
    # Always serve the current built dashboard HTML; avoid stale browser/proxy copies after deploy.
    return FileResponse("static/index.html", headers={"Cache-Control":"no-store, no-cache, must-revalidate", "Pragma":"no-cache"})

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
    all_states = sorted([{"state": name, "amount_crore": vals[-1]} for name, vals in AGRI_CREDIT_DISBURSEMENT.items()],
                        key=lambda x: x["amount_crore"], reverse=True)
    top = all_states[:10]
    outstanding = AGRI_CREDIT_OUTSTANDING.get(key, (None, None, None))
    groups = [
        {"bank_group": "Scheduled commercial banks", "amount_crore": outstanding[0]},
        {"bank_group": "Regional rural banks", "amount_crore": outstanding[1]},
        {"bank_group": "Rural cooperative banks", "amount_crore": outstanding[2]},
    ]
    reported = [x["amount_crore"] for x in groups if x["amount_crore"] is not None]
    data = {
        "state": state, "selected_district": district,
        "selected_fy": fy,
        "selected_fy_disbursement": next((row for row in history if row["financial_year"] == fy), None),
        "selected_fy_data_available": any(row["financial_year"] == fy for row in history),
        "data_level": "State/UT aggregate; not district-specific",
        "disbursement_history": history, "top_states_disbursement": top, "all_states_disbursement": all_states,
        "outstanding_by_bank_group": groups,
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


# Published benchmark/final district estimates transcribed from the ISSRF report's
# West Bengal table (Annexure-1, page A1.44) and Jaipur entry in Rajasthan (A1.32).
# These are old modelled household-income estimates, never current farm profit.
INCOME_FALLBACK_ROWS = [
    ("West Bengal","Alipurduar",6485.06,13.90), ("West Bengal","Bankura",5518.50,12.38),
    ("West Bengal","Birbhum",7318.56,9.05), ("West Bengal","Dakshin Dinajpur",6359.60,14.77),
    ("West Bengal","Darjiling",9753.37,2.93), ("West Bengal","Haora",6391.29,14.74),
    ("West Bengal","Hugli",6262.41,7.81), ("West Bengal","Jalpaiguri",7740.07,16.55),
    ("West Bengal","Jhargram",4867.54,4.44), ("West Bengal","Kalimpong",7983.53,2.83),
    ("West Bengal","Koch Bihar",5160.93,11.70), ("West Bengal","Maldah",6626.23,11.79),
    ("West Bengal","Murshidabad",6523.76,8.10), ("West Bengal","Nadia",6536.62,10.92),
    ("West Bengal","North Twenty Four Parganas",6211.21,10.62),
    ("West Bengal","Paschim Barddhaman",6236.75,21.77),
    ("West Bengal","Paschim Medinipur",6024.12,12.88),
    ("West Bengal","Purba Barddhaman",5489.24,14.95),
    ("West Bengal","Purba Medinipur",7689.92,8.33), ("West Bengal","Puruliya",7171.03,11.56),
    ("West Bengal","South Twenty Four Parganas",7370.79,7.40), ("West Bengal","Uttar Dinajpur",9078.78,5.89),
    ("Rajasthan","Jaipur",13023.27,13.76),
]
def _income_fallback():
    return [{"state":state, "district":district, "survey_income_rs":None, "linear_mixed_income_rs":None,
             "spatial_income_rs":None, "survey_rse":None, "linear_mixed_rse":rse, "spatial_rse":None,
             "monthly_income_rs":income, "reference_period":"July 2018–June 2019"}
            for state,district,income,rse in INCOME_FALLBACK_ROWS]

def _extract_income_rows_from_pdf():
    """Read district income appendix; records remain tagged as model estimates, not profit."""
    try:
        response = HTTP.get(AGRI_INCOME_PDF, timeout=75)
        response.raise_for_status()
        reader = PdfReader(BytesIO(response.content), strict=False)
    except Exception:
        logging.exception("Could not download/read ISSRF income PDF; use only pre-verified rows as fallback")
        return _income_fallback()
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
    # Merge manually validated rows so the principal default selection can still render
    # even when the host's PDF extraction is incomplete. Prefer PDF records on duplicates.
    seen_keys = {(_norm(r["state"]), _norm(r["district"])) for r in dedup}
    for row in _income_fallback():
        key = (_norm(row["state"]), _norm(row["district"]))
        if key not in seen_keys:
            dedup.append(row); seen_keys.add(key)
    logging.info("Parsed %s district income rows from ISSRF PDF plus verified fallback rows", len(dedup))
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
            "state": state, "district": selected["district"], "selected_fy": fy,
            "selected_fy_data_available": False,
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


def official_pmfby_national_summary():
    """Published all-India Kharif snapshot from the official PMFBY administrative dashboard."""
    # Source page last updated 28 September 2026, 09:09 AM. 2026 is an in-progress
    # season: its displayed zero in the claim row is not treated as a final outcome.
    years = ["2018","2019","2020","2021","2022","2023","2024","2025","2026"]
    farmers = [23256797,21578164,18906275,16845752,20058814,25268201,29629227,22976591,27298609]
    gross_premium_lakh = [2117238,2387258,2068650,1892150,2005257,2034766,2077966,1415036,1236114]
    claims_paid_lakh = [1987878,2208868,1446423,1502087,1324476,1641203,1134726,987259,None]
    sum_insured_lakh = [13990538,14538353,11625528,10208166,12706726,16781176,18199735,14505586,18560267]
    area_pmfby_thousand_ha = [30745.39,32150.52,28210.43,24789.65,26331.53,31413.93,32746.10,25977.69,28400.50]
    area_rwbcis_thousand_ha = [1602.58,1576.76,346.04,334.91,371.49,1119.74,1214.40,960.11,1237.50]
    history = []
    for i, year in enumerate(years):
        history.append({
            "kharif_year": year,
            "reporting_status": "in_progress" if year == "2026" else "published_dashboard_value",
            "farmers_shown": farmers[i],
            "gross_premium_crore": round(gross_premium_lakh[i] / 100, 2),
            "claims_paid_crore": round(claims_paid_lakh[i] / 100, 2) if claims_paid_lakh[i] is not None else None,
            "sum_insured_crore": round(sum_insured_lakh[i] / 100, 2),
            "area_insured_pmfby_thousand_hectares": area_pmfby_thousand_ha[i],
            "area_insured_rwbcis_thousand_hectares": area_rwbcis_thousand_ha[i],
        })
    latest = history[-1]
    return result("PMFBY / RWBCIS official administrative dashboard (All-India Kharif)", "available",
                  {"scope":"All India; Kharif season; PMFBY administrative-dashboard figures, not state/district values",
                   "history":history,"latest_reported_year":latest,
                   "unit_note":"Published financial figures are in ₹ lakh; converted to ₹ crore by dividing by 100. Insured area is in thousand hectares.",
                   "snapshot_last_updated":"2026-09-28 09:09 AM",
                   "in_progress_year":"2026",
                   "in_progress_note":"The 2026 Kharif season is in progress in this snapshot. Claims shown as zero on the source are not treated as a final zero; claim amount is marked not final here."},
                  updated="2026-09-28",
                  note="Source: Ministry of Agriculture & Farmers Welfare, PMFBY Administrative Dashboard, Kharif season, last updated 28 September 2026 at 09:09 AM. This is an All-India scheme aggregate, not selected-state/district data. The 2026 Kharif claims figure is intentionally marked not final rather than treating the dashboard's displayed zero as a completed outcome. Gross premium and sum insured are conversions from the published ₹ lakh values.",
                  url="https://pmfby.gov.in/adminStatistics/dashboard")

# Historical agricultural-loan recovery data from the Ministry of Agriculture Lok Sabha
# answer dated 2 Jan 2018. Amounts in the published annexure are ₹ lakh.
# StCB records cover reported 2017 audited data for 24/33 banks; DCCB records are
# state-level, reported as at 30 Jun 2017 for 283/370 DCCBs. Do NOT describe as current.
STCB_RECOVERY_2017 = {
    "Andaman and Nicobar Islands": (11922.12, 8008.06, 67.17),
    "Andhra Pradesh": (83240.75, 81899.17, 98.39),
    "Assam": (6405.96, 2649.48, 41.36),
    "Bihar": (130884.08, 122071.52, 93.27),
    "Chandigarh": (1898.50, 1554.38, 81.87),
    "Chhattisgarh": (50487.90, 38997.54, 77.24),
    "Delhi": (44318.17, 42402.41, 95.68),
    "Gujarat": (271995.58, 263896.78, 97.02),
    "Haryana": (687985.00, 687722.00, 99.96),
    "Himachal Pradesh": (20802.92, 10156.31, 48.82),
    "Jammu and Kashmir": (7282.28, 5280.02, 72.51),
    "Karnataka": (645674.89, 624913.15, 96.78),
    "Kerala": (743361.00, 691507.95, 93.02),
    "Madhya Pradesh": (676128.88, 606206.94, 89.66),
    "Maharashtra": (890759.84, 744864.01, 83.62),
    "Meghalaya": (10710.57, 2066.02, 19.29),
    "Mizoram": (17681.23, 11011.69, 62.28),
    "Odisha": (634429.26, 622139.95, 98.06),
    "Puducherry": (36895.76, 34368.99, 93.15),
    "Punjab": (715601.69, 713487.26, 99.70),
    "Rajasthan": (634026.06, 632705.94, 99.79),
    "Tamil Nadu": (627372.33, 620137.80, 98.85),
    "Telangana": (242876.05, 242356.73, 99.79),
    "Uttar Pradesh": (447719.14, 428442.56, 95.69),
}
DCCB_RECOVERY_2017 = {
    "Andhra Pradesh": (735664.34, 653279.37, 88.80),
    "Bihar": (49813.30, 17052.20, 34.23),
    "Chhattisgarh": (465601.53, 331270.86, 71.15),
    "Gujarat": (1323089.54, 1165744.25, 88.11),
    "Haryana": (1059788.42, 718460.33, 67.79),
    "Himachal Pradesh": (98579.02, 68676.89, 69.67),
    "Jammu and Kashmir": (22374.73, 11927.22, 53.31),
    "Jharkhand": (1313893.37, 1194776.08, 90.93),
    "Karnataka": (1523842.79, 1082983.05, 71.07),
    "Kerala": (1419850.20, 892302.49, 62.84),
    "Madhya Pradesh": (3190011.08, 1454859.66, 45.61),
    "Maharashtra": (1106846.28, 812026.56, 73.36),
    "Odisha": (1067183.47, 812154.39, 76.10),
    "Punjab": (1525588.99, 1260295.21, 82.61),
    "Rajasthan": (1181975.84, 1036525.67, 87.69),
    "Tamil Nadu": (444719.76, 400269.25, 90.00),
    "Telangana": (552275.35, 157997.12, 28.61),
    "Uttarakhand": (76242.69, 29800.27, 39.09),
    "Uttar Pradesh": (147532.13, 103547.43, 70.19),
    "West Bengal": (286368.50, 239256.88, 83.55),
}

def official_loan_recovery_gap(state: str, district: str, fy: str):
    """Return historical official state-level cooperative-bank recoveries, never a derived current figure."""
    stcb_name = next((name for name in STCB_RECOVERY_2017 if _norm(name) == _norm(state)), None)
    dccb_name = next((name for name in DCCB_RECOVERY_2017 if _norm(name) == _norm(state)), None)
    records = []
    if stcb_name:
        demand, recovered, rate = STCB_RECOVERY_2017[stcb_name]
        records.append({
            "bank_group": "State Cooperative Bank (StCB)",
            "period_label": "Audited reporting period in 2017",
            "demand_lakh": demand, "recovered_lakh": recovered,
            "demand_crore": round(demand / 100, 4),
            "recovered_crore": round(recovered / 100, 4),
            "recovery_rate_percent": rate,
            "reference_date": "2017",
            "coverage_note": "Published audited records for 24 of 33 State Cooperative Banks."
        })
    if dccb_name:
        demand, recovered, rate = DCCB_RECOVERY_2017[dccb_name]
        records.append({
            "bank_group": "District Central Cooperative Banks (DCCBs)",
            "period_label": "Report as at 30 June 2017",
            "demand_lakh": demand, "recovered_lakh": recovered,
            "demand_crore": round(demand / 100, 4),
            "recovered_crore": round(recovered / 100, 4),
            "recovery_rate_percent": rate,
            "reference_date": "2017-06-30",
            "coverage_note": "Published state total from 283 of 370 DCCBs."
        })
    comparisons = [
        {"state": name, "recovery_rate_percent": v[2], "recovered_crore": round(v[1] / 100, 4)}
        for name, v in DCCB_RECOVERY_2017.items()
    ]
    comparisons.sort(key=lambda x: x["recovery_rate_percent"], reverse=True)
    data = {
        "state": state, "selected_district": district,
        "data_level": "state-level cooperative-bank records; not district-level",
        "current_year_recovery_data": None,
        "selected_fy": fy, "selected_fy_data_available": False,
        "records": records, "dccb_state_comparison": comparisons,
        "reference_period": "2017 historical data",
        "official_answer_date": "2018-01-02",
        "source_date": "2017-06-30",
        "amount_unit": "INR crore (converted from the source's INR lakh)"
    }
    if not records:
        return result(
            "Historical agricultural loan recovery / collection (cooperative banks)", "no_matching_records",
            data,
            updated="2018-01-02",
            note="The linked official 2017 recovery tables do not report a cooperative-bank recovery record for this selected state. No current recovery amount or district amount is inferred.",
            url="https://sansad.in/getFile/loksabhaquestions/annex/13/AU2329.pdf?source=pqals"
        )
    return result(
        "Historical agricultural loan recovery / collection (cooperative banks)", "available",
        data, updated="2018-01-02",
        note="Historical only: Government of India, Ministry of Agriculture, Lok Sabha Unstarred Question 2329 answered 2 January 2018. StCB demand/collection data are reported for 2017; DCCB state totals are reported as at 30 June 2017. Amounts are converted from the source's ₹ lakh to ₹ crore. These are cooperative-bank records, not current total agricultural loan repayments, and the source does not report district-wise recovery.",
        url="https://sansad.in/getFile/loksabhaquestions/annex/13/AU2329.pdf?source=pqals"
    )


IMD_DISTRICT_RSS = "https://mausam.imd.gov.in/imd_latest/contents/dist_nowcast_rss.php"
IMD_DISTRICT_MAP = "https://mausam.imd.gov.in/responsive/districtWiseWarningGIS.php"

def _imd_item_text(item, tag):
    for child in list(item):
        if child.tag.split("}")[-1].casefold() == tag.casefold():
            return (child.text or "").strip()
    return ""

# District names that appear in more than one state in the dashboard administrative directory.
IMD_AMBIGUOUS_DISTRICT_NAMES = {"balrampur", "bilaspur", "hamirpur", "pratapgarh"}

def _imd_exact_district_title(title: str, district: str) -> bool:
    """Only accept an exact district label, never a match buried in forecast prose."""
    title_norm = _norm(title)
    district_norm = _norm(district)
    if not title_norm or not district_norm:
        return False
    # Do not apply state-free aliases: the nationwide RSS does not consistently
    # include state metadata, so loose aliases can display a different district.
    accepted = {district_norm}
    # Labels may be followed only by the word "district"; forecast description text
    # is deliberately never used for geographic matching.
    return title_norm in accepted or title_norm in {name + "district" for name in accepted}


def imd_public_warnings(state: str, district: str):
    """Return only selected-district warnings; never use unrelated RSS items as alerts."""
    source = "India Meteorological Department (IMD) district nowcast/warnings"
    api_url = "https://api.imd.gov.in/api/v1/districtwarning"

    # Prefer structured API records with district and, when provided, state metadata.
    try:
        response = HTTP.get(api_url, timeout=15, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        rows = _items(payload.get("data", payload.get("records", payload.get("warnings", payload))) if isinstance(payload, dict) else payload)
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_district = _field(row, "District", "district", "district_name", "districtName", "DISTRICT")
            row_state = _field(row, "State", "state", "state_name", "stateName", "STATE")
            if _norm(row_district) != _norm(district):
                continue
            # When state metadata is present, require it to match.
            # When absent, only accept district names unique in the administrative directory.
            if row_state:
                if _norm(row_state) != _norm(state):
                    continue
            elif _norm(district) in IMD_AMBIGUOUS_DISTRICT_NAMES:
                continue
            matches.append(row)
        if matches:
            return result(
                source, "available",
                {"state": state, "district": district, "district_match": True,
                 "state_match": bool(_field(matches[0], "State", "state", "state_name", "stateName", "STATE")),
                 "feed": "IMD district-wise warning API", "records": matches[:10]},
                updated=date.today().isoformat(),
                note="Only structured records whose district name matches the selected district are shown. If the provider includes state metadata, it must also match the selected state. Review the issue time and validity before acting.",
                url=api_url
            )
    except Exception:
        logging.info("IMD JSON warning API unavailable or returned no verified match; checking RSS title labels")

    # RSS is a nationwide feed. Its title typically contains the district name, while
    # the description contains generic weather language. Match the title exactly and
    # reject a record when explicit state metadata conflicts with the selected state.
    try:
        response = HTTP.get(IMD_DISTRICT_RSS, timeout=18,
                            headers={"Accept": "application/rss+xml, application/xml, text/xml"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = [el for el in root.iter() if el.tag.split("}")[-1].casefold() == "item"]
        parsed, matches = [], []
        for item in items:
            title = _imd_item_text(item, "title")
            description = _imd_item_text(item, "description")
            published = _imd_item_text(item, "pubDate")
            link = _imd_item_text(item, "link") or _imd_item_text(item, "guid") or IMD_DISTRICT_MAP
            row_state = (
                _imd_item_text(item, "state") or _imd_item_text(item, "state_name")
                or _imd_item_text(item, "stateName") or _imd_item_text(item, "state_ut")
            )
            if not title:
                continue
            rec = {"title": title, "description": description, "published": published,
                   "link": link, "district": district, "state": row_state or None}
            parsed.append(rec)
            if not _imd_exact_district_title(title, district):
                continue
            # Use explicit state metadata when available; otherwise only accept a
            # district name unique in the administrative directory.
            if row_state:
                if _norm(row_state) != _norm(state):
                    continue
            elif _norm(district) in IMD_AMBIGUOUS_DISTRICT_NAMES:
                continue
            matches.append(rec)

        # Do not treat a national feed's latest items as warnings for this district.
        # Multiple same-name records without state metadata are ambiguous and are withheld.
        if matches:
            return result(
                source, "available",
                {"state": state, "district": district, "district_match": True,
                 "state_match": bool(matches[0].get("state")),
                 "feed": "IMD district nowcast RSS", "records": matches[:5],
                 "feed_items_checked": len(parsed)},
                updated=date.today().isoformat(),
                note="The alert title exactly matched the selected district. Where the feed supplies state metadata, it was checked against the selected state. If state metadata is absent, only a district name unique in the administrative directory is accepted; ambiguous names are withheld. Review issue time and validity.",
                url=IMD_DISTRICT_RSS
            )
        return result(
            source, "reference_available",
            {"state": state, "district": district, "district_match": False,
             "records": [], "feed_items_checked": len(parsed),
             "official_district_warning_map": IMD_DISTRICT_MAP},
            updated=date.today().isoformat(),
            note="No verified alert matched this selected state/district in the public feed. This does NOT mean there is no warning. Unmatched districts from other states are intentionally not shown; check the official district-wise warning map.",
            url=IMD_DISTRICT_MAP
        )
    except Exception as e:
        logging.exception("IMD public RSS unavailable")
        return result(
            source, "reference_available",
            {"state": state, "district": district, "district_match": False,
             "records": [], "official_district_warning_map": IMD_DISTRICT_MAP,
             "official_nowcast_rss": IMD_DISTRICT_RSS},
            note=f"The public RSS could not be read ({type(e).__name__}). No warning has been inferred for the selected district; use the official IMD district-wise warning map.",
            url=IMD_DISTRICT_MAP
        )


def official_finance_reference(state: str, district: str, fy: str):
    """Verified PM-KISAN scheme terms; this is a scheme reference, never a district payment claim."""
    return result("PM-KISAN official scheme reference", "reference_available",
        {"programme":"Pradhan Mantri Kisan Samman Nidhi (PM-KISAN)",
         "annual_assistance_inr":6000,
         "equal_installments_per_year":3,
         "installment_value_inr_derived":2000,
         "funding_source":"100% Government of India",
         "scheme_start_date":"2018-12-01",
         "beneficiary_basis":"Eligible landholding farmer families, subject to scheme guidelines and exclusion categories",
         "data_level":"National scheme terms",
         "selected_geography":{"state":state,"district":district,"financial_year":fy},
         "district_payment_data_status":"Not provided by this scheme-reference page"},
        note="Official PM-KISAN scheme terms. The ₹2,000 installment value is arithmetic derived from ₹6,000 per year in three equal installments; it is not a claim that the selected district or any farmer received a payment. Eligibility exclusions and state/UT verification apply.",
        url="https://pmkisan.gov.in/")

# PMFBY/RWBCIS state-wise 5-year snapshot transcribed from Government of India,
# Lok Sabha Starred Question 121, answered 9 Dec 2025; data as on 31 Oct 2025.
# Units in the source: applications/farmer-claims in lakh; area in lakh ha;
# sum insured, farmers' premium, and claims in ₹ crore. A dash means not reported.
PMFBY_STATE_SUMMARIES = {
    "Andaman and Nicobar Islands": {"applications_insured_lakh": 0.0, "area_insured_lakh_ha": 0.0, "sum_insured_crore": 6.1, "farmers_premium_crore": 0.0, "paid_claims_crore": 0.0, "farmers_claims_paid_lakh": 0.0},
    "Andhra Pradesh": {"applications_insured_lakh": 348.8, "area_insured_lakh_ha": 110.1, "sum_insured_crore": 99031.4, "farmers_premium_crore": 32.9, "paid_claims_crore": 747.1, "farmers_claims_paid_lakh": 14.6},
    "Assam": {"applications_insured_lakh": 51.0, "area_insured_lakh_ha": 30.4, "sum_insured_crore": 23501.6, "farmers_premium_crore": 19.3, "paid_claims_crore": 605.7, "farmers_claims_paid_lakh": 8.5},
    "Bihar": {"applications_insured_lakh": None, "area_insured_lakh_ha": None, "sum_insured_crore": None, "farmers_premium_crore": None, "paid_claims_crore": None, "farmers_claims_paid_lakh": None},
    "Chhattisgarh": {"applications_insured_lakh": 349.3, "area_insured_lakh_ha": 118.8, "sum_insured_crore": 51701.0, "farmers_premium_crore": 1031.0, "paid_claims_crore": 3709.2, "farmers_claims_paid_lakh": 82.2},
    "Goa": {"applications_insured_lakh": 0.0, "area_insured_lakh_ha": 0.0, "sum_insured_crore": 0.9, "farmers_premium_crore": 0.0, "paid_claims_crore": 0.0, "farmers_claims_paid_lakh": 0.0},
    "Gujarat": {"applications_insured_lakh": None, "area_insured_lakh_ha": None, "sum_insured_crore": None, "farmers_premium_crore": None, "paid_claims_crore": None, "farmers_claims_paid_lakh": None},
    "Haryana": {"applications_insured_lakh": 330.1, "area_insured_lakh_ha": 68.9, "sum_insured_crore": 51590.1, "farmers_premium_crore": 1439.9, "paid_claims_crore": 5957.8, "farmers_claims_paid_lakh": 66.8},
    "Himachal Pradesh": {"applications_insured_lakh": 13.6, "area_insured_lakh_ha": 224.8, "sum_insured_crore": 3942.8, "farmers_premium_crore": 152.6, "paid_claims_crore": 374.9, "farmers_claims_paid_lakh": 5.7},
    "Jammu and Kashmir": {"applications_insured_lakh": 6.4, "area_insured_lakh_ha": 2.9, "sum_insured_crore": 2241.4, "farmers_premium_crore": 41.3, "paid_claims_crore": 119.9, "farmers_claims_paid_lakh": 2.1},
    "Jharkhand": {"applications_insured_lakh": 27.0, "area_insured_lakh_ha": 13.6, "sum_insured_crore": 9662.4, "farmers_premium_crore": 0.3, "paid_claims_crore": None, "farmers_claims_paid_lakh": None},
    "Karnataka": {"applications_insured_lakh": 130.0, "area_insured_lakh_ha": 104.2, "sum_insured_crore": 55832.7, "farmers_premium_crore": 1584.2, "paid_claims_crore": 10006.2, "farmers_claims_paid_lakh": 74.7},
    "Kerala": {"applications_insured_lakh": 7.2, "area_insured_lakh_ha": 3.2, "sum_insured_crore": 2575.9, "farmers_premium_crore": 50.4, "paid_claims_crore": 573.0, "farmers_claims_paid_lakh": 4.0},
    "Madhya Pradesh": {"applications_insured_lakh": 717.5, "area_insured_lakh_ha": 516.3, "sum_insured_crore": 206092.4, "farmers_premium_crore": 3708.1, "paid_claims_crore": 13863.4, "farmers_claims_paid_lakh": 210.3},
    "Maharashtra": {"applications_insured_lakh": 792.5, "area_insured_lakh_ha": 501.1, "sum_insured_crore": 232654.1, "farmers_premium_crore": 2684.2, "paid_claims_crore": 26342.0, "farmers_claims_paid_lakh": 369.1},
    "Manipur": {"applications_insured_lakh": 0.2, "area_insured_lakh_ha": 0.1, "sum_insured_crore": 89.8, "farmers_premium_crore": 1.8, "paid_claims_crore": 6.7, "farmers_claims_paid_lakh": 0.1},
    "Meghalaya": {"applications_insured_lakh": 0.9, "area_insured_lakh_ha": 0.3, "sum_insured_crore": 254.8, "farmers_premium_crore": 0.1, "paid_claims_crore": 24.0, "farmers_claims_paid_lakh": 0.3},
    "Odisha": {"applications_insured_lakh": 547.9, "area_insured_lakh_ha": 63.9, "sum_insured_crore": 46769.5, "farmers_premium_crore": 450.6, "paid_claims_crore": 2579.5, "farmers_claims_paid_lakh": 84.7},
    "Puducherry": {"applications_insured_lakh": 1.8, "area_insured_lakh_ha": 0.6, "sum_insured_crore": 413.7, "farmers_premium_crore": 0.1, "paid_claims_crore": 10.8, "farmers_claims_paid_lakh": 0.3},
    "Rajasthan": {"applications_insured_lakh": 1598.8, "area_insured_lakh_ha": 533.1, "sum_insured_crore": 226623.8, "farmers_premium_crore": 4568.4, "paid_claims_crore": 18851.2, "farmers_claims_paid_lakh": 389.8},
    "Sikkim": {"applications_insured_lakh": 0.1, "area_insured_lakh_ha": 0.0, "sum_insured_crore": 12.7, "farmers_premium_crore": 0.4, "paid_claims_crore": 0.0, "farmers_claims_paid_lakh": 0.0},
    "Tamil Nadu": {"applications_insured_lakh": 287.6, "area_insured_lakh_ha": 76.3, "sum_insured_crore": 48786.4, "farmers_premium_crore": 809.9, "paid_claims_crore": 5829.9, "farmers_claims_paid_lakh": 117.7},
    "Telangana": {"applications_insured_lakh": None, "area_insured_lakh_ha": None, "sum_insured_crore": None, "farmers_premium_crore": None, "paid_claims_crore": None, "farmers_claims_paid_lakh": None},
    "Tripura": {"applications_insured_lakh": 13.4, "area_insured_lakh_ha": 2.5, "sum_insured_crore": 1735.6, "farmers_premium_crore": 2.1, "paid_claims_crore": 9.7, "farmers_claims_paid_lakh": 1.2},
    "Uttar Pradesh": {"applications_insured_lakh": 293.9, "area_insured_lakh_ha": 138.1, "sum_insured_crore": 81726.2, "farmers_premium_crore": 1461.8, "paid_claims_crore": 3311.7, "farmers_claims_paid_lakh": 58.4},
    "Uttarakhand": {"applications_insured_lakh": 11.1, "area_insured_lakh_ha": 214.5, "sum_insured_crore": 5643.2, "farmers_premium_crore": 257.2, "paid_claims_crore": 968.5, "farmers_claims_paid_lakh": 6.9},
    "West Bengal": {"applications_insured_lakh": 0.0, "area_insured_lakh_ha": None, "sum_insured_crore": None, "farmers_premium_crore": None, "paid_claims_crore": None, "farmers_claims_paid_lakh": None},
}

# District-level crop insurance coverage/claims for Karnataka from Government of India,
# Lok Sabha Unstarred Question 2588, answered 16 Dec 2025; coverage 2020-21 to 2024-25,
# figures as on 31 Oct 2025. Financial values are ₹ crore; area in lakh hectares.
PMFBY_KARNATAKA_DISTRICT = {
    "Bagalkot": (201427, 2.30, 1382.15, 42.54, 295.75, 97704),
    "Ballari": (75844, 0.72, 455.57, 11.03, 44.67, 28428),
    "Belagavi": (328700, 2.73, 1716.61, 53.55, 361.22, 145225),
    "Bengaluru Rural": (41526, 0.17, 96.14, 2.81, 10.93, 16605),
    "Bengaluru Urban": (3091, 0.01, 6.78, 0.20, 1.07, 1287),
    "Bidar": (1294302, 8.38, 3151.56, 62.90, 232.26, 608296),
    "Chamarajanagar": (77990, 0.40, 155.42, 3.34, 15.62, 27694),
    "Chikkaballapur": (210129, 0.98, 587.00, 15.23, 151.21, 143757),
    "Chikkamagaluru": (248281, 1.34, 1220.73, 58.99, 278.67, 141991),
    "Chitradurga": (581527, 6.94, 3443.35, 80.15, 559.91, 248212),
    "Dakshina Kannada": (857238, 2.22, 2458.46, 122.82, 880.55, 517156),
    "Davangere": (267241, 2.00, 1414.33, 43.70, 335.32, 171939),
    "Dharwad": (862748, 9.05, 3990.59, 114.10, 534.18, 428555),
    "Gadag": (1081988, 13.25, 5781.39, 144.39, 1119.02, 725367),
    "Hassan": (509126, 2.22, 970.31, 28.97, 186.89, 255647),
    "Haveri": (1454731, 11.01, 6186.39, 147.99, 1256.37, 1082578),
    "Kalaburagi": (676778, 7.95, 3440.31, 72.57, 730.45, 799443),
    "Kodagu": (14545, 0.09, 55.09, 2.57, 9.95, 7596),
    "Kolar": (93469, 0.45, 233.68, 8.78, 35.66, 50272),
    "Koppal": (580117, 6.38, 2815.63, 61.09, 301.81, 265259),
    "Mandya": (295869, 0.82, 307.24, 5.77, 71.53, 178456),
    "Mysuru": (41662, 0.18, 74.67, 1.72, 7.07, 15356),
    "Raichur": (336102, 4.00, 2347.83, 52.73, 153.97, 93764),
    "Ramanagara": (60998, 0.31, 184.92, 7.77, 57.90, 37583),
    "Shivamogga": (437205, 2.24, 2304.14, 98.83, 496.72, 299067),
    "Tumakuru": (646346, 3.84, 2099.58, 63.28, 250.80, 313788),
    "Udupi": (62774, 0.16, 181.25, 8.86, 55.70, 33702),
    "Uttara Kannada": (757386, 2.37, 2006.07, 76.08, 305.18, 421818),
    "Vijayanagara": (181837, 1.64, 909.24, 18.77, 219.80, 140485),
    "Vijayapura": (654244, 9.37, 5452.65, 162.54, 1015.20, 360493),
    "Yadgir": (59865, 0.73, 403.58, 10.15, 30.79, 29406),
}
PMFBY_DISTRICT_ALIASES = {
    "chamarajnagar": "Chamarajanagar", "kalaburgi": "Kalaburagi",
    "uttarkannada": "Uttara Kannada", "yadgiri": "Yadgir",
    "hasan": "Hassan", "dakshinakannada": "Dakshina Kannada",
}

def official_agricultural_insurance(state: str, district: str, fy: str):
    """Published PMFBY/RWBCIS multi-year snapshot; district-level rows only where the official annexure supplies them."""
    state_row = next((dict(row, state=name) for name, row in PMFBY_STATE_SUMMARIES.items()
                      if _norm(name) == _norm(state)), None)
    district_name = district
    for alias, canonical in PMFBY_DISTRICT_ALIASES.items():
        if _norm(alias) == _norm(district):
            district_name = canonical
            break
    district_data = None
    if _norm(state) == _norm("Karnataka"):
        key = next((name for name in PMFBY_KARNATAKA_DISTRICT
                    if _norm(name) == _norm(district_name)), None)
        if key:
            applications, area, insured, farmer_premium, claims, farmers_benefitted = PMFBY_KARNATAKA_DISTRICT[key]
            district_data = {
                "district": key, "applications_enrolled": applications, "area_insured_lakh_ha": area,
                "sum_insured_crore": insured, "farmers_premium_crore": farmer_premium,
                "paid_claims_crore": claims, "farmers_benefitted": farmers_benefitted,
                "geographic_scope": "district",
            }
    comparisons = []
    for name, row in PMFBY_STATE_SUMMARIES.items():
        if row.get("sum_insured_crore") is not None:
            comparisons.append({"state": name, "sum_insured_crore": row["sum_insured_crore"]})
    comparisons.sort(key=lambda x: x["sum_insured_crore"], reverse=True)
    claims_comparison = [{"state": name, "paid_claims_crore": row["paid_claims_crore"]}
                         for name, row in PMFBY_STATE_SUMMARIES.items()
                         if row.get("paid_claims_crore") is not None]
    claims_comparison.sort(key=lambda x: x["paid_claims_crore"], reverse=True)
    all_districts = []
    if _norm(state) == _norm("Karnataka"):
        all_districts = [
            {"district": name, "applications_enrolled": vals[0], "area_insured_lakh_ha": vals[1],
             "sum_insured_crore": vals[2], "farmers_premium_crore": vals[3],
             "paid_claims_crore": vals[4], "farmers_benefitted": vals[5]}
            for name, vals in PMFBY_KARNATAKA_DISTRICT.items()
        ]
        all_districts.sort(key=lambda row: row["sum_insured_crore"], reverse=True)
    if not state_row and not district_data:
        return result(
            "PMFBY/RWBCIS agriculture crop-insurance coverage and claims", "no_matching_records",
            {"state": state, "district": district, "financial_year_selector": fy,
             "district_record": None, "state_summary": None,
             "data_level": "unavailable for selected state in this linked official snapshot",
             "reference_period": "2020-21 to 2024-25", "source_as_of": "2025-10-31",
             "state_comparison_sum_insured": comparisons, "state_comparison_claims_paid": claims_comparison,
             "district_records": []},
            note="The linked official parliamentary snapshot does not contain a numeric insurance record for this selected state. No values are inferred; the source can be opened to verify the reported coverage limits.",
            updated="2025-12-16",
            url="https://sansad.in/getFile/loksabhaquestions/annex/186/AS121_xU9ufj.pdf?source=pqals"
        )
    if state_row is None and district_data:
        state_row = {"state": state, "applications_insured_lakh": None, "area_insured_lakh_ha": None,
                     "sum_insured_crore": None, "farmers_premium_crore": None,
                     "paid_claims_crore": None, "farmers_claims_paid_lakh": None}
    data_level = "district" if district_data else "state/UT aggregate"
    state_scheme_info = None
    if _norm(state) == _norm("West Bengal"):
        state_scheme_info = {
            "name": "Bangla Shasya Bima",
            "url": "https://admin-banglashasyabima.wb.gov.in/insurance_calculations",
            "note": "West Bengal's state crop-insurance calculator requires season, year, district, block, crop and insured area. It does not provide one universal district sum-insured amount."
        }
    note_scope = ("District row matched in the official Karnataka annexure." if district_data else
                  "Only a state/UT aggregate is published for this selected geography in the linked source; no district-level amount is inferred.")
    if state == "West Bengal" and state_row.get("applications_insured_lakh") == 0.0:
        note_scope += " The source reports 0.0 lakh applications insured and dashes for the other metrics; dashes are preserved as not reported, not converted to zero."
    return result(
        "PMFBY/RWBCIS agriculture crop-insurance coverage and claims", "available",
        {"state": state, "district": district, "data_level": data_level,
         "selected_fy": fy, "selected_fy_data_available": False,
         "period_is_five_year_cumulative": True,
         "reference_period": "2020-21 to 2024-25", "source_as_of": "2025-10-31",
         "state_summary": state_row, "district_record": district_data,
         "state_scheme_info": state_scheme_info,
         "state_comparison_sum_insured": comparisons, "state_comparison_claims_paid": claims_comparison,
         "district_records": all_districts,
         "numeric_precision": "State values as printed to one decimal in the parliamentary annexure; Karnataka district values as printed to two decimals in ₹ crore."},
        updated="2025-12-16",
        note="Source: Government of India, Lok Sabha Starred Question 121 answered 9 December 2025; PMFBY/RWBCIS applications insured, area insured, sum insured, farmers' premium and claims-paid totals for 2020-21 to 2024-25, as on 31 October 2025. State-level summary is supplied where the annexure lists it. District-wise rows in the linked official annexure are available for Karnataka; other selected districts fall back to their state's reported aggregate where present. This is a five-year cumulative snapshot, not the selected FY alone. A dash in the source means not reported.",
        url="https://sansad.in/getFile/loksabhaquestions/annex/186/AS121_xU9ufj.pdf?source=pqals"
    )


@app.get("/api/source/{name}")
def dashboard_source(name: str, state: str, district: str, fy: str, commodity: str = Query("Wheat", min_length=2, max_length=60)):
    """Return one independent dashboard source so the browser can render panels as they finish."""
    def insurance_source():
        # Use only the explicitly verified PMFBY reference, never a free-text filtered OGD dataset.
        return official_agricultural_insurance(state,district,fy)
    jobs = {
        "climate": lambda: open_meteo_climate(state,district,fy),
        "agriculture": lambda: ceda_agri_market(state,district,fy,commodity),
        "agri_credit": lambda: official_agriculture_credit(state,district,fy),
        "farm_income": lambda: agricultural_household_income(state,district,fy),
        "loan_recovery": lambda: official_loan_recovery_gap(state,district,fy),
        "pmfby_national": lambda: official_pmfby_national_summary(),
        "pm_kisan": lambda: official_finance_reference(state,district,fy),
        "insurance": insurance_source,
        "imd_current": lambda: imd_public_warnings(state,district),
    }
    if name not in jobs:
        return result("Unknown dashboard source", "error", note="No source with that name is configured.")
    try:
        return cached(f"source:{name}:{state}:{district}:{fy}:{commodity}", jobs[name])
    except Exception as e:
        logging.exception("Dashboard source %s failed", name)
        return result(name, "error", note=f"Source request failed: {type(e).__name__}. Other dashboard panels remain available.")


@app.get("/api/dashboard")
def dashboard(state: str, district: str, fy: str, commodity: str = "Wheat"):
    key=f"dash:{state}:{district}:{fy}:{commodity}"
    def load():
        sources={}
        def safe_call(name, fn, source_name):
            try:
                return name, fn()
            except Exception as e:
                logging.exception("%s source failed", name)
                return name, result(source_name, "error", note=f"Source request failed: {type(e).__name__}. Other dashboard sections remain available.")

        def insurance_source():
            # Use only the explicitly verified PMFBY reference, never a free-text filtered OGD dataset.
            return official_agricultural_insurance(state,district,fy)

        jobs = [
            ("climate", lambda: open_meteo_climate(state,district,fy), "Open-Meteo historical weather"),
            ("agriculture", lambda: ceda_agri_market(state,district,fy,commodity), "Agricultural market prices (AGMARKNET)"),
            ("agri_credit", lambda: official_agriculture_credit(state,district,fy), "Official agricultural credit"),
            ("farm_income", lambda: agricultural_household_income(state,district,fy), "Agricultural household income"),
            ("loan_recovery", lambda: official_loan_recovery_gap(state,district,fy), "Loan recovery"),
            ("pmfby_national", lambda: official_pmfby_national_summary(), "PMFBY national summary"),
            ("pm_kisan", lambda: official_finance_reference(state,district,fy), "PM-KISAN agricultural income support"),
            ("insurance", insurance_source, "Crop insurance / risk finance"),
            ("imd_current", lambda: imd_public_warnings(state,district), "IMD district nowcast/warnings"),
        ]
        # Fetch independent sources concurrently so one slow provider does not keep
        # every dashboard panel blank while the other sources are already available.
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures=[pool.submit(safe_call,name,fn,source_name) for name,fn,source_name in jobs]
            for future in as_completed(futures):
                name, payload = future.result()
                sources[name]=payload

        return {"selection":{"state":state,"district":district,"fy":fy},"generated_at":date.today().isoformat(),"sources":sources,
          "disclaimer":"Credit disbursement is state/UT-level from a Ministry of Finance Lok Sabha answer dated 23 March 2026, not district-specific. Outstanding loans are provisional as of 31 December 2025. Agricultural-household income is a model-based estimate for July 2018–June 2019, not farm profit or current revenue. Loan repayments and farm-business profit are not inferred where verified data are unavailable. CEDA/AGMARKNET reports mandi prices, not crop production. Climate uses gridded weather estimates."}
    return cached(key,load)

@app.get("/api/config")
def config():
    return {"agmarknet_price_source":"CEDA/AGMARKNET integration",
            "official_agriculture_credit_available":True,
            "pm_kisan_scheme_reference_available":True,
            "pmfby_kharif_snapshot_available":True,
            "message":"Dashboard panels are limited to agriculture, farm-weather risk context, agricultural household benchmarks, agricultural credit, farmer support, and crop insurance. Scheme references are not presented as district payment records; state and national values are labeled by geographic scope."}
