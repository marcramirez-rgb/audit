"""Fleet catalog data layer for the Snowflake-backed unit picker (v2.0).

Powers the "Fleet Picker" tab in audit_gui.py: a cascading
Client -> Location -> TDC selector that resolves the chosen live units into the
exact camera rows the audit engine consumes, so a TAM can audit without ever
hand-typing an IP.

Every resolved row uses the SAME six columns the CSV path already relies on --
``IP``, ``LIVE_UNIT_SERIAL_NM``, ``LOCATION_NM``, ``CLIENT_NM``,
``MANUFACTURER``, ``MODEL`` -- so the picker feeds ``camera_engine.run_batch``
identically to a browsed CSV. The picker is just a second way to build that row
list; it does not replace CSV upload.

Two interchangeable backends sit behind one ``CatalogSource`` interface:

  * ``SnowflakeSource``   -- live queries via snowflake-connector-python using
                            SSO / externalbrowser auth (no shared secret stored
                            in the app; the browser handles the LVT login).
  * ``CachedCatalogSource`` -- reads a periodically-exported catalog CSV so the
                            picker still works when the DB driver can't install
                            or the network is locked down. The catalog CSV has
                            the same columns as an audit CSV, so an existing
                            fleet export doubles as the cache.

The GUI never imports ``snowflake`` directly -- it only touches this module, and
``build_source()`` decides which backend is available.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

# The six columns every consumer downstream expects. Kept here so both backends
# and the GUI agree on one spelling.
AUDIT_COLUMNS = ["IP", "LIVE_UNIT_SERIAL_NM", "LOCATION_NM", "CLIENT_NM", "MANUFACTURER", "MODEL"]

# Where the offline catalog export lives by default. Override with the
# FLEET_CATALOG_PATH env var. Kept next to the app so a coworker can drop the
# exported file in without editing anything.
DEFAULT_CATALOG_FILENAME = "fleet_catalog.csv"


class CatalogError(Exception):
    """Raised for any catalog/source failure the GUI should surface verbatim."""


class CatalogSource:
    """Interface both backends implement. All list methods return sorted,
    de-duplicated, non-blank values so the GUI can bind them straight to a
    dropdown."""

    #: Short human label shown in the GUI so the operator knows where the data
    #: is coming from ("Live Snowflake (SSO)" vs "Cached catalog: fleet.csv").
    label = "unknown source"

    def list_clients(self):
        raise NotImplementedError

    def list_locations(self, client):
        raise NotImplementedError

    def list_units(self, client, location):
        """Return the live-unit serials (TDCxxxxx) at one client+location."""
        raise NotImplementedError

    def resolve_cameras(self, serials):
        """Given a list of unit serials, return the camera rows to audit --
        one dict per camera, keyed by AUDIT_COLUMNS. A unit typically has up to
        three cameras, so len(result) >= len(serials)."""
        raise NotImplementedError

    def close(self):
        """Release any held connection. Safe to call more than once."""


# --------------------------------------------------------------------------- #
# Offline backend: a flat catalog CSV loaded into memory.
# --------------------------------------------------------------------------- #

class CachedCatalogSource(CatalogSource):
    """Serves the cascade from a local catalog CSV.

    The CSV must carry at least CLIENT_NM, LOCATION_NM, LIVE_UNIT_SERIAL_NM, and
    IP; MANUFACTURER and MODEL are used if present (blank otherwise). One row per
    camera -- i.e. the exact shape of an audit CSV, so an existing fleet export
    works as-is.
    """

    def __init__(self, catalog_path):
        self.catalog_path = Path(catalog_path)
        self.label = f"Cached catalog: {self.catalog_path.name}"
        self._rows = self._load()

    def _load(self):
        if not self.catalog_path.exists():
            raise CatalogError(
                f"Catalog file not found:\n{self.catalog_path}\n\n"
                "Export a fleet catalog CSV (CLIENT_NM, LOCATION_NM, "
                "LIVE_UNIT_SERIAL_NM, IP, MANUFACTURER, MODEL) and place it "
                "there, or set FLEET_CATALOG_PATH."
            )
        try:
            with open(self.catalog_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames or []
                rows = [self._normalize(r) for r in reader]
        except OSError as e:
            raise CatalogError(f"Could not read catalog '{self.catalog_path.name}':\n{e}") from e

        required = {"CLIENT_NM", "LOCATION_NM", "LIVE_UNIT_SERIAL_NM", "IP"}
        missing = required - set(fieldnames)
        if missing:
            raise CatalogError(
                f"Catalog '{self.catalog_path.name}' is missing required column(s): "
                f"{', '.join(sorted(missing))}.\nFound: {fieldnames}"
            )
        rows = [r for r in rows if r["IP"]]
        if not rows:
            raise CatalogError(f"Catalog '{self.catalog_path.name}' has no rows with an IP.")
        return rows

    @staticmethod
    def _normalize(raw):
        return {col: (raw.get(col, "") or "").strip() for col in AUDIT_COLUMNS}

    @staticmethod
    def _sorted_unique(values):
        return sorted({v for v in values if v}, key=str.casefold)

    def list_clients(self):
        return self._sorted_unique(r["CLIENT_NM"] for r in self._rows)

    def list_locations(self, client):
        return self._sorted_unique(
            r["LOCATION_NM"] for r in self._rows if r["CLIENT_NM"] == client
        )

    def list_units(self, client, location):
        return self._sorted_unique(
            r["LIVE_UNIT_SERIAL_NM"]
            for r in self._rows
            if r["CLIENT_NM"] == client and r["LOCATION_NM"] == location
        )

    def resolve_cameras(self, serials):
        wanted = set(serials)
        return [dict(r) for r in self._rows if r["LIVE_UNIT_SERIAL_NM"] in wanted]


# --------------------------------------------------------------------------- #
# Live backend: Snowflake via SSO / externalbrowser.
# --------------------------------------------------------------------------- #

class SnowflakeSource(CatalogSource):
    """Live cascade against Snowflake.

    Mirrors the query in
    ``.claude/skills/camera-audit/references/snowflake_query.md``: the cascade
    lists distinct CLIENT_NM / LOCATION_NM / LIVE_UNIT_SERIAL_NR from
    ``EDW.DM_PRODUCT.LIVE_UNIT``, and ``resolve_cameras`` joins that to
    ``STAGING.HORUS.NETWORK_CAMERAS`` + ``CAMERA_MFTRS`` to get the camera IP,
    manufacturer, and model.

    Auth is SSO/externalbrowser: on the first query the connector opens the
    system browser for the LVT login, so no password/key is stored in the app.
    Only SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER are required from the environment;
    warehouse/role/database are optional.
    """

    label = "Live Snowflake (SSO)"

    def __init__(self, connect_params=None):
        self._connect_params = connect_params or _snowflake_params_from_env()
        self._conn = None  # opened lazily on first query

    # -- connection management -------------------------------------------- #

    def _connect(self):
        if self._conn is not None:
            return self._conn
        # NB: no truststore / custom CA handling here. Snowflake's endpoint uses a
        # publicly-trusted cert (the corporate proxy MITMs pypi but not
        # *.snowflakecomputing.com), so the connector's bundled CAs verify fine.
        # Injecting truststore here previously caused a fatal recursion with the
        # connector's vendored urllib3 ("250003: maximum recursion depth
        # exceeded") -- don't reintroduce it.
        try:
            import snowflake.connector  # deferred: app must launch without the driver
        except ImportError as e:
            raise CatalogError(
                "snowflake-connector-python is not installed, so the live "
                "picker is unavailable. Install it, or switch the picker to the "
                "cached catalog."
            ) from e
        missing = [k for k in ("account", "user") if not self._connect_params.get(k)]
        if missing:
            raise CatalogError(
                "Missing Snowflake config: "
                + ", ".join(f"SNOWFLAKE_{k.upper()}" for k in missing)
                + ".\nSet them in .env (see .env.example) or switch to the cached catalog."
            )
        try:
            self._conn = snowflake.connector.connect(
                authenticator="externalbrowser",
                client_session_keep_alive=True,
                # Fail cleanly if the SSO/browser step isn't completed, instead
                # of hanging the picker's load thread indefinitely.
                login_timeout=90,
                network_timeout=90,
                **self._connect_params,
            )
        except Exception as e:  # snowflake raises many subclasses; surface them
            raise CatalogError(f"Could not connect to Snowflake:\n{e}") from e
        return self._conn

    def _query(self, sql, params=()):
        conn = self._connect()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                return cur.fetchall(), [d[0] for d in cur.description]
            finally:
                cur.close()
        except CatalogError:
            raise
        except Exception as e:
            raise CatalogError(f"Snowflake query failed:\n{e}") from e

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # -- cascade ----------------------------------------------------------- #

    def list_clients(self):
        rows, _ = self._query(
            "SELECT DISTINCT CLIENT_NM FROM EDW.DM_PRODUCT.LIVE_UNIT "
            "WHERE CLIENT_NM IS NOT NULL ORDER BY 1"
        )
        return [r[0] for r in rows if r[0]]

    def list_locations(self, client):
        rows, _ = self._query(
            "SELECT DISTINCT LOCATION_NM FROM EDW.DM_PRODUCT.LIVE_UNIT "
            "WHERE CLIENT_NM = %s AND LOCATION_NM IS NOT NULL ORDER BY 1",
            (client,),
        )
        return [r[0] for r in rows if r[0]]

    def list_units(self, client, location):
        rows, _ = self._query(
            "SELECT DISTINCT LIVE_UNIT_SERIAL_NR FROM EDW.DM_PRODUCT.LIVE_UNIT "
            "WHERE CLIENT_NM = %s AND LOCATION_NM = %s "
            "AND LIVE_UNIT_SERIAL_NR IS NOT NULL ORDER BY 1",
            (client, location),
        )
        return [r[0] for r in rows if r[0]]

    def resolve_cameras(self, serials):
        serials = list(serials)
        if not serials:
            return []
        placeholders = ", ".join(["%s"] * len(serials))
        sql = (
            "SELECT nc.PUBLIC_IP AS IP, "
            "       lu.LIVE_UNIT_SERIAL_NR AS LIVE_UNIT_SERIAL_NM, "
            "       lu.LOCATION_NM, lu.CLIENT_NM, "
            "       cm.MANUFACTURER, cm.MODEL "
            "FROM STAGING.HORUS.NETWORK_CAMERAS nc "
            "JOIN EDW.DM_PRODUCT.LIVE_UNIT lu ON nc.LIVE_UNIT_ID = lu.LIVE_UNIT_ID "
            "LEFT JOIN STAGING.HORUS.CAMERA_MFTRS cm ON nc.CAMERA_MFTRS_ID = cm.ID "
            f"WHERE lu.LIVE_UNIT_SERIAL_NR IN ({placeholders}) "
            "GROUP BY lu.CLIENT_NM, lu.LOCATION_NM, nc.PUBLIC_IP, "
            "         lu.LIVE_UNIT_SERIAL_NR, cm.MANUFACTURER, cm.MODEL "
            "ORDER BY lu.LOCATION_NM"
        )
        rows, cols = self._query(sql, tuple(serials))
        out = []
        for r in rows:
            record = dict(zip(cols, r))
            row = {col: ("" if record.get(col) is None else str(record.get(col)).strip())
                   for col in AUDIT_COLUMNS}
            if row["IP"]:
                out.append(row)
        return out


# --------------------------------------------------------------------------- #
# Source selection
# --------------------------------------------------------------------------- #

def _snowflake_params_from_env():
    """Read Snowflake connection settings from the environment. Only account +
    user are required; the rest are passed through when present."""
    params = {
        "account": os.environ.get("SNOWFLAKE_ACCOUNT", "").strip(),
        "user": os.environ.get("SNOWFLAKE_USER", "").strip(),
    }
    for env_key, param_key in (
        ("SNOWFLAKE_WAREHOUSE", "warehouse"),
        ("SNOWFLAKE_ROLE", "role"),
        ("SNOWFLAKE_DATABASE", "database"),
        ("SNOWFLAKE_SCHEMA", "schema"),
    ):
        val = os.environ.get(env_key, "").strip()
        if val:
            params[param_key] = val
    return {k: v for k, v in params.items() if v}


def default_catalog_path():
    override = os.environ.get("FLEET_CATALOG_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / DEFAULT_CATALOG_FILENAME


def snowflake_configured():
    """True if enough env config exists to *attempt* a live connection. Does not
    prove the driver is installed or the login works -- just that it's worth
    offering 'Live' as the default."""
    p = _snowflake_params_from_env()
    return bool(p.get("account") and p.get("user"))


def snowflake_driver_available():
    try:
        import snowflake.connector  # noqa: F401
        return True
    except ImportError:
        return False


def build_source(prefer="auto", catalog_path=None):
    """Return a ready CatalogSource.

    prefer:
      "live"   -> SnowflakeSource (raises via first query if unusable).
      "cache"  -> CachedCatalogSource from catalog_path / default.
      "auto"   -> live when the driver is present AND env is configured,
                  otherwise the cached catalog.

    Raises CatalogError if the chosen source can't be constructed (e.g. cache
    requested but the file is missing).
    """
    if prefer == "live":
        return SnowflakeSource()
    if prefer == "cache":
        return CachedCatalogSource(catalog_path or default_catalog_path())
    # auto
    if snowflake_driver_available() and snowflake_configured():
        return SnowflakeSource()
    return CachedCatalogSource(catalog_path or default_catalog_path())
