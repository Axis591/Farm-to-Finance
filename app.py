import os, time, logging
from datetime import date
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
    start,end=fy_dates(fy)
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
                  updated=date.today().isoformat(),note="Gridded/reanalysis estimate for the geocoded location; not an official district aggregate or a formal climate-risk score.",url="https://open-meteo.com/")

def ogd_resource(resource_id: str, state: str, district: str, fy: str, label: str):
    key=os.getenv("DATA_GOV_IN_API_KEY","").strip()
    if not key or not resource_id:
        return result(label,"not_configured",note="Configure DATA_GOV_IN_API_KEY and the matching resource ID in the server environment. No value has been fabricated.",url="https://data.gov.in/")
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
    except Exception as e:
        logging.exception("OGD fetch failed")
        return result(label,"error",note=f"Provider request failed: {type(e).__name__}. Check resource ID, key, network access, and provider limits.",url="https://data.gov.in/")

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
        sources["agriculture"]=ogd_resource(os.getenv("OGD_AGRICULTURE_RESOURCE_ID",""),state,district,fy,"Agriculture / crop statistics (OGD)")
        sources["finance"]=ogd_resource(os.getenv("OGD_FINANCE_RESOURCE_ID",""),state,district,fy,"Public finance / scheme data (OGD)")
        sources["insurance"]=ogd_resource(os.getenv("OGD_INSURANCE_RESOURCE_ID",""),state,district,fy,"Crop insurance / risk finance (OGD)")
        # IMD public API: live warnings/observations are context, not historical FY data.
        try:
            r=HTTP.get("https://api.imd.gov.in/api/v1/districtwarning",timeout=20)
            r.raise_for_status()
            raw=r.json()
            records=raw if isinstance(raw,list) else raw.get("data",raw.get("records",[])) if isinstance(raw,dict) else []
            matched=[x for x in records if district.lower() in str(x.get("District",x.get("district",""))).lower()]
            sources["imd_current"]=result("India Meteorological Department (IMD) district warnings","available" if matched else "no_matching_records",
                 {"records":matched[:10]},updated=date.today().isoformat(),
                 note="Current warning feed, not a historical FY dataset. Matching is by district name and may need official district IDs.",url="https://api.imd.gov.in/public/api_reference.html")
        except Exception as e:
            sources["imd_current"]=result("India Meteorological Department (IMD) district warnings","unavailable",note="Feed unavailable from this server or endpoint access was restricted.",url="https://api.imd.gov.in/public/api_reference.html")
        return {"selection":{"state":state,"district":district,"fy":fy},"generated_at":date.today().isoformat(),"sources":sources,
          "disclaimer":"No configured OGD resources means agriculture and finance figures are unavailable, not zero. Climate data is a location-based gridded estimate, not a certified district statistic. Do not treat modelled indicators as official financial or climate-risk ratings."}
    return cached(key,load)

@app.get("/api/config")
def config():
    return {"ogd_key_configured":bool(os.getenv("DATA_GOV_IN_API_KEY","").strip()),
            "agriculture_resource_configured":bool(os.getenv("OGD_AGRICULTURE_RESOURCE_ID","").strip()),
            "finance_resource_configured":bool(os.getenv("OGD_FINANCE_RESOURCE_ID","").strip()),
            "insurance_resource_configured":bool(os.getenv("OGD_INSURANCE_RESOURCE_ID","").strip()),
            "message":"Set API credentials and resource IDs in the server environment to activate these connectors."}
