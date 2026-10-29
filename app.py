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

def _records(payload):
    """Extract record arrays from common JSON response envelopes."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "items", "commodities", "geographies", "states", "districts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = _records(value)
                if nested:
                    return nested
    return []

def _field(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None

def ceda_agri_market(state: str, district: str, fy: str):
    """Fetch market price context from CEDA's public Agri Market Data portal.
    These are mandi prices, not crop-production totals or official district yields.
    """
    base = "https://agmarknet.ceda.ashoka.edu.in/api"
    source = "CEDA Agri Market Data (AGMARKNET mandi prices)"
    try:
        commodities_r = HTTP.get(f"{base}/commodities", timeout=20)
        commodities_r.raise_for_status()
        commodities = _records(commodities_r.json())
        if not commodities:
            return result(source, "unavailable", note="CEDA responded, but its commodity list had an unrecognized format. No values were inferred.", url="https://agmarknet.ceda.ashoka.edu.in/")

        # Wheat is a transparent default so the feed can be tested without adding
        # another user input; it is clearly identified in the returned data/note.
        commodity = next((x for x in commodities if str(_field(x, "name", "commodity", "cmdty", "commodity_name", "label") or "").strip().casefold() == "wheat"), None)
        if not commodity:
            return result(source, "unavailable", note="CEDA's commodity list did not include a recognizable Wheat entry. No market-price value was inferred.", url="https://agmarknet.ceda.ashoka.edu.in/")

        geographies_r = HTTP.get(f"{base}/geographies", timeout=20)
        geographies_r.raise_for_status()
        geography_payload = geographies_r.json()
        geographies = _records(geography_payload)
        if not geographies:
            # Some portal versions expose state and district lookups separately.
            states_r = HTTP.get(f"{base}/states", timeout=20)
            states_r.raise_for_status()
            geographies = _records(states_r.json())

        state_norm = state.strip().casefold()
        state_row = next((x for x in geographies if str(_field(x, "state", "state_name", "name", "label") or "").strip().casefold() == state_norm), None)
        state_id = _field(state_row or {}, "state_id", "id", "stateId", "value")
        district_row = None

        # If geography data is hierarchical, search children. Otherwise try the
        # documented-style district lookup with the resolved state identifier.
        if state_row:
            children = _records(state_row)
            district_norm = district.strip().casefold()
            district_row = next((x for x in children if str(_field(x, "district", "district_name", "name", "label") or "").strip().casefold() == district_norm), None)
        if state_id is not None and district_row is None:
            for path in (f"{base}/districts?state_id={state_id}", f"{base}/districts?state={state_id}"):
                try:
                    dr = HTTP.get(path, timeout=20)
                    dr.raise_for_status()
                    rows = _records(dr.json())
                    district_row = next((x for x in rows if str(_field(x, "district", "district_name", "name", "label") or "").strip().casefold() == district.strip().casefold()), None)
                    if district_row:
                        break
                except requests.exceptions.RequestException:
                    continue
        district_id = _field(district_row or {}, "district_id", "id", "districtId", "value")
        if state_id is None or district_id is None:
            return result(source, "unavailable", note="CEDA market data is reachable, but the selected state/district could not be mapped confidently to its geographic IDs. No other district was substituted.", url="https://agmarknet.ceda.ashoka.edu.in/")

        today = date.today()
        start = (today - timedelta(days=365)).isoformat()
        end = today.isoformat()
        payload = {
            "state_id": state_id,
            "district_id": district_id,
            "commodity_id": _field(commodity, "commodity_id", "id", "commodityId", "value"),
            "calculation_type": "d",
            "start_date": start,
            "end_date": end,
        }
        prices_r = HTTP.post(f"{base}/prices", json=payload, timeout=30)
        prices_r.raise_for_status()
        price_rows = _records(prices_r.json())
        normalized = []
        for row in price_rows:
            normalized.append({
                "commodity": _field(row, "cmdty", "commodity", "commodity_name") or "Wheat",
                "date": _field(row, "t", "date", "arrival_date"),
                "minimum_price_rs_per_quintal": _field(row, "p_min", "min_price", "minimum_price"),
                "maximum_price_rs_per_quintal": _field(row, "p_max", "max_price", "maximum_price"),
                "modal_price_rs_per_quintal": _field(row, "p_modal", "modal_price", "price"),
            })
        normalized = [x for x in normalized if any(x[k] is not None for k in ("minimum_price_rs_per_quintal", "maximum_price_rs_per_quintal", "modal_price_rs_per_quintal"))]
        normalized.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
        latest = normalized[0] if normalized else None
        return result(
            source, "available" if latest else "no_matching_records",
            {"commodity": "Wheat", "state": state, "district": district, "latest_available_record": latest, "recent_records": normalized[:12],
             "price_unit": "INR per quintal", "requested_period_start": start, "requested_period_end": end},
            updated=(latest.get("date") if latest else None),
            note="CEDA Agri Market Data, sourced from AGMARKNET. This panel currently uses Wheat as a default test commodity and shows the latest record returned for the selected district; it reports mandi prices, not crop area or production. Data may lag; verify the record date before decisions. Credit: Centre for Economic Data & Analysis (CEDA), Ashoka University.",
            url="https://agmarknet.ceda.ashoka.edu.in/",
        )
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "unknown"
        logging.exception("CEDA Agri Market HTTP error")
        return result(source, "error", note=f"CEDA's market-data service returned HTTP {code}. Try again later; no substitute values were generated.", url="https://agmarknet.ceda.ashoka.edu.in/")
    except requests.exceptions.Timeout:
        logging.exception("CEDA Agri Market request timed out")
        return result(source, "error", note="CEDA market-data request timed out. Try again later; no substitute values were generated.", url="https://agmarknet.ceda.ashoka.edu.in/")
    except (requests.exceptions.RequestException, ValueError, TypeError) as e:
        logging.exception("CEDA Agri Market fetch failed")
        return result(source, "error", note=f"Could not read CEDA market data ({type(e).__name__}). The dashboard will not invent agriculture values.", url="https://agmarknet.ceda.ashoka.edu.in/")

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
    # Use geocoding as a discovery source, not as an authoritative district register.
    # Return likely India locations in the selected state; UI also allows manual district entry.
    try:
        r=HTTP.get("https://geocoding-api.open-meteo.com/v1/search",params={"name":state,"count":100,"language":"en","format":"json"},timeout=15)
        r.raise_for_status()
        found=[]
        for x in r.json().get("results",[]):
            if x.get("country_code")=="IN":
                n=x.get("name")
                if n and n not in found: found.append(n)
        return {"districts":sorted(found),"source":"Open-Meteo geocoding suggestions","note":"Suggestions only; this is not an authoritative district list. Type the district if it is missing."}
    except Exception:
        return {"districts":[],"source":"unavailable","note":"District suggestions unavailable; type the district name."}

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
          "disclaimer":"Agriculture now uses CEDA Agri Market Data (AGMARKNET) for mandi prices, with Wheat as the current default test commodity; these are not crop-production totals. Finance and insurance remain separate OGD feeds. Climate data is a location-based gridded estimate, not a certified district statistic. Do not treat modelled indicators as official financial or climate-risk ratings."}
    return cached(key,load)

@app.get("/api/config")
def config():
    return {"ogd_key_configured":bool(os.getenv("DATA_GOV_IN_API_KEY","").strip()),
            "agriculture_resource_configured":bool(os.getenv("OGD_AGRICULTURE_RESOURCE_ID","").strip()),
            "finance_resource_configured":bool(os.getenv("OGD_FINANCE_RESOURCE_ID","").strip()),
            "insurance_resource_configured":bool(os.getenv("OGD_INSURANCE_RESOURCE_ID","").strip()),
            "message":"Set API credentials and resource IDs in the server environment to activate these connectors."}
