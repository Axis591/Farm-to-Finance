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

def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

def _items(payload):
    """Normalize list/dict/string JSON shapes used by the public AGMARKNET portal API."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "items", "commodities", "states", "districts"):
            value = payload.get(key)
            if isinstance(value, list): return value
            if isinstance(value, dict):
                nested = _items(value)
                if nested: return nested
        return [dict(v, name=k) if isinstance(v, dict) else {"name":k,"id":v} for k,v in payload.items() if isinstance(v, (dict, str, int))]
    return []

def _field(row, *keys):
    if isinstance(row, str):
        return row if any(_norm(row) == _norm(k) for k in keys) else None
    if not isinstance(row, dict): return None
    normalized = {_norm(k): v for k, v in row.items()}
    for key in keys:
        value = normalized.get(_norm(key))
        if value not in (None, ""): return value
    return None

CEDA_LEGACY_API = "https://agmarknet.ceda.ashoka.edu.in/api"

def _ceda_get(path, params=None):
    response = HTTP.get(f"{CEDA_LEGACY_API}/{path.lstrip('/')}", params=params or {}, timeout=25)
    response.raise_for_status()
    return response.json()

def _ceda_states():
    return cached("ceda:legacy:states", lambda: _items(_ceda_get("states")))

def _state_row(state):
    target = _norm(state)
    for row in _ceda_states():
        name = row if isinstance(row, str) else _field(row, "state", "state_name", "name", "label", "stateName", "state_name_english")
        if _norm(name) == target:
            return row
    # Common official-name aliases used by older AGMARKNET data
    aliases = {"odisha":"orissa", "uttarakhand":"uttaranchal", "delhi":"nct of delhi",
               "dadraanddamananddiu":"dadraandnagarhavelianddamananddiu"}
    alias = aliases.get(target)
    if alias:
        for row in _ceda_states():
            name = row if isinstance(row, str) else _field(row, "state", "state_name", "name", "label", "stateName")
            if _norm(name) == alias: return row
    return None

def ceda_districts_for_state(state: str):
    try:
        row = _state_row(state)
        if row is None: return []
        state_id = row if isinstance(row, (int, str)) and not isinstance(row, str) else _field(row, "state_id", "stateId", "id", "value", "state_code")
        if state_id is None: return []
        payload = _ceda_get("districts", {"state_id": state_id})
        found = []
        for item in _items(payload):
            name = item if isinstance(item, str) else _field(item, "district", "district_name", "name", "label", "districtName")
            if name and str(name).strip() not in found: found.append(str(name).strip())
        return sorted(found, key=str.casefold)
    except Exception:
        logging.exception("AGMARKNET district lookup failed")
        return []

def ceda_agri_market(state: str, district: str, fy: str):
    """Use the Government of India's AGMARKNET OGD API; no CEDA key or guessed IDs required."""
    source = "Government of India AGMARKNET mandi prices (data.gov.in)"
    resource_id = "9ef84268-d588-465a-a308-a864a43d0070"
    api_key = os.getenv("DATA_GOV_IN_API_KEY", "").strip()
    source_url = "https://www.data.gov.in/resource/current-daily-price-various-commodities-various-markets-mandi"
    if not api_key:
        return result(source, "not_configured",
                      note="Add a valid DATA_GOV_IN_API_KEY in Render. The agriculture resource ID is already built in; you do not need to find or enter an agriculture UUID.",
                      url=source_url)
    try:
        start, fy_end = fy_dates(fy)
        today = date.today()
        end = min(fy_end, today.isoformat())
        if start > end:
            return result(source, "unavailable", note=f"FY {fy} has no dates available yet.", url=source_url)
        # OGD's current daily mandi-price resource. Request the selected geography
        # directly; the provider returns current-day records, not a full historical archive.
        params = {
            "api-key": api_key, "format": "json", "limit": 1000, "offset": 0,
            "filters[state]": state, "filters[district]": district,
        }
        response = HTTP.get(f"https://api.data.gov.in/resource/{resource_id}", params=params, timeout=35)
        response.raise_for_status()
        payload = response.json()
        records = payload.get("records", []) if isinstance(payload, dict) else []
        if not isinstance(records, list):
            records = []
        selected = []
        for row in records:
            if not isinstance(row, dict):
                continue
            row_state = _field(row, "state", "state_name")
            row_district = _field(row, "district", "district_name")
            if _norm(row_state) != _norm(state) or _norm(row_district) != _norm(district):
                continue
            commodity = _field(row, "commodity", "commodity_name")
            arrival = _field(row, "arrival_date", "arrivalDate", "date")
            if not arrival:
                continue
            try:
                observed = date.fromisoformat(str(arrival)[:10])
            except ValueError:
                try: observed = datetime.strptime(str(arrival), "%d/%m/%Y").date()
                except ValueError: continue
            if not (date.fromisoformat(start) <= observed <= date.fromisoformat(end)):
                continue
            selected.append({
                "state": row_state, "district": row_district,
                "market": _field(row, "market", "market_name"),
                "commodity": commodity,
                "variety": _field(row, "variety"),
                "arrival_date": observed.isoformat(),
                "minimum_price_rs_per_quintal": _field(row, "min_price", "minimum_price"),
                "maximum_price_rs_per_quintal": _field(row, "max_price", "maximum_price"),
                "modal_price_rs_per_quintal": _field(row, "modal_price", "modalprice"),
            })
        selected.sort(key=lambda x: x["arrival_date"], reverse=True)
        latest = selected[0] if selected else None
        # This public OGD resource is a current daily feed, so older FYs are not
        # claimed as covered by it. Explain that limitation instead of faking history.
        if not latest and start < today.isoformat():
            note = ("The Government OGD API responded, but its current daily mandi-price feed returned no records for this state/district in the selected FY. "
                    "This resource is not a historical FY archive. Choose the current FY or connect a historical source; no values were inferred.")
        else:
            note = ("Official Government of India AGMARKNET mandi-price feed, filtered to the selected state, district and FY date window. "
                    "This is current daily market data, not crop production or a full historical series. Verify arrival date before decisions.")
        return result(source, "available" if latest else "no_matching_records",
                      {"state":state,"district":district,"financial_year":fy,"latest_available_record":latest,
                       "records":selected[:100],"records_returned":len(selected),"period_start":start,"period_end":end,
                       "price_unit":"INR per quintal"},
                      updated=(latest.get("arrival_date") if latest else None), note=note, url=source_url)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "unknown"
        logging.exception("AGMARKNET OGD HTTP error")
        note = "Government OGD rejected the API request."
        if code in (401, 403): note = "Government OGD rejected the API key. Replace DATA_GOV_IN_API_KEY in Render with a valid key."
        elif code == 404: note = "The built-in AGMARKNET resource could not be found. Check provider availability."
        return result(source, "error", note=f"{note} (HTTP {code}).", url=source_url)
    except requests.exceptions.Timeout:
        logging.exception("AGMARKNET OGD timed out")
        return result(source, "error", note="Government OGD API timed out. Try again later.", url=source_url)
    except (requests.exceptions.RequestException, ValueError, TypeError) as e:
        logging.exception("AGMARKNET OGD fetch failed")
        return result(source, "error", note=f"Could not read Government OGD response ({type(e).__name__}).", url=source_url)

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
    uuid_pattern = r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    return {"ogd_key_configured":bool(os.getenv("DATA_GOV_IN_API_KEY","").strip()),
            "agriculture_resource_configured":True,
            "finance_resource_configured":bool(re.fullmatch(uuid_pattern, os.getenv("OGD_FINANCE_RESOURCE_ID","").strip())),
            "insurance_resource_configured":bool(re.fullmatch(uuid_pattern, os.getenv("OGD_INSURANCE_RESOURCE_ID","").strip())),
            "message":"Agriculture uses the built-in Government AGMARKNET resource. Finance and insurance require their own real data.gov.in resource UUIDs plus DATA_GOV_IN_API_KEY."}
