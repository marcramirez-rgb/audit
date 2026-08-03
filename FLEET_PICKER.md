# Fleet Picker (v2.0)

Pick a camera by **Client → Location → TDC** instead of hand-typing IPs. Works
in both tools:

- **Audit tool** (`audit_gui.py`) — a **Fleet Picker** tab alongside Single
  Camera and CSV Batch. Multi-select TDCs and a **basket that spans multiple
  clients**, so you can build a bulk audit without a CSV (or on top of one). CSV
  upload is unchanged — the picker is just a second way in.
- **Analytics Writer** (`analytics_writer_gui.py`) — a **"Pick from fleet…"**
  button that fills the camera's IP + vendor. You still set the port
  (5010/5015/5020 = Center/Left/Right) yourself, since the catalog doesn't track
  which position you're editing.

## Two data sources

The picker reads from whichever is available (choose **Auto / Live Snowflake /
Cached catalog** in the UI):

1. **Live Snowflake** — queries the fleet directly. Auth is **SSO /
   externalbrowser**: the first query opens your browser for the LVT (Okta)
   login, so no password or key is stored in the app. `setup.bat` installs the
   driver; to add it to an existing venv (the `--use-feature=truststore` flag is
   just so pip itself gets through the corporate proxy):
   ```bash
   .venv\Scripts\python -m pip install --use-feature=truststore "snowflake-connector-python[secure-local-storage]"
   ```
   and in `.env`:
   ```
   SNOWFLAKE_ACCOUNT=BWXPHOE-LVT
   SNOWFLAKE_USER=you@lvt.com
   SNOWFLAKE_WAREHOUSE=PRODUCT_WH   # optional
   SNOWFLAKE_ROLE=APP-SNOWFLAKE-PRODUCT_ANALYST   # optional
   ```
   `SNOWFLAKE_ACCOUNT` must be the **org-account** identifier (`BWXPHOE-LVT`),
   not the account locator (`MZA23640…`) — the locator URL 404s on the auth
   endpoint. `[secure-local-storage]` caches the SSO token so you're not
   re-prompted every launch. (No `truststore` needed — Snowflake's cert is
   publicly trusted; injecting truststore actually recurses with the connector's
   vendored urllib3.)

2. **Cached catalog** — an offline CSV export, for when the driver can't install
   or the network is locked down. Point `FLEET_CATALOG_PATH` at it, or drop it
   next to the app as `fleet_catalog.csv`. Real catalogs are gitignored (they
   hold client names + site addresses) — don't commit them.

`Auto` uses live only when the driver is installed **and** the env is set;
otherwise it falls back to the cached catalog.

## Cached catalog format

Same six columns as an audit CSV, so an existing fleet export works as-is. One
row per camera. Copy this into `fleet_catalog.csv` to try it offline:

```csv
CLIENT_NM,LOCATION_NM,LIVE_UNIT_SERIAL_NM,IP,MANUFACTURER,MODEL
Acme Retail - USA,Dallas DC,TDC14016,10.23.66.205,Axis,P3265
Acme Retail - USA,Dallas DC,TDC14016,10.23.66.207,Hikvision,DS-2CD
Acme Retail - USA,Phoenix Yard,TDC15001,10.24.10.5,Axis,P3265
Globex Logistics,Newark Hub,TDC22010,10.30.1.100,Hikvision,DS-2CD
Globex Logistics,Newark Hub,TDC22011,10.30.1.110,LVT,LVT-Cam
```

Only `CLIENT_NM`, `LOCATION_NM`, `LIVE_UNIT_SERIAL_NM`, and `IP` are required;
`MANUFACTURER`/`MODEL` fill in vendor detection when present.
