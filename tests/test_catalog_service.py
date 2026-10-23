"""Exercise the installed Astroquery/PyVO HTTP boundary against a loopback TAP server."""

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from astropy import units as u
from astropy.table import Table
from astroquery.simbad import SimbadClass
from pyvo.dal import TAPService

from hvs_catalog.catalogue.simbad import AstroquerySimbad
from hvs_catalog.records.catalog import SimbadQuery


@pytest.mark.integration
@pytest.mark.parametrize("failed", [False, True])
def test_astroquery_basic_and_photometry_round_trip(failed):
    requests = []
    basic = Table(
        {
            "oid": [123],
            "main_id": ["HD 1"],
            "ids": ["HD 1|HIP 2"],
            "ra": [10.0],
            "dec": [0.0],
            "pmra": [3.0],
            "pm_err_maj": [0.1],
            "pm_bibcode": ["FictionalPM"],
            "plx_value": [0.2],
            "plx_err": [0.01],
            "rvz_radvel": [520.0],
            "rvz_bibcode": ["FictionalRV"],
            "sp_type": ["B"],
        }
    )
    basic["ra"].unit = u.deg
    basic["dec"].unit = u.deg
    basic["rvz_radvel"].unit = u.km / u.s
    flux = Table(
        {
            "filter": ["V"],
            "flux": [12.0],
            "flux_err": [0.1],
            "bibcode": ["FictionalPhotometry"],
        }
    )
    flux["flux"].unit = u.mag

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            params = parse_qs(body)
            query = params["QUERY"][0]
            requests.append(query)
            if failed:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"Service unavailable")
                return
            table = flux if "FROM flux" in query else basic
            output = io.BytesIO()
            table.write(output, format="votable")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-votable+xml")
            self.end_headers()
            self.wfile.write(output.getvalue())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    class LocalSimbad(SimbadClass):
        @property
        def tap(self):
            return self.local_tap

    client = LocalSimbad()
    client._hardlimit = 10000
    client.local_tap = TAPService(
        f"http://127.0.0.1:{server.server_port}", session=client._session
    )
    try:
        response = AstroquerySimbad(client).query(
            SimbadQuery(method="identifier", identifier="HD 1")
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    if failed:
        assert response.error and not response.candidates
        assert "Service unavailable" in response.raw_response
    else:
        assert response.error is None
        assert len(requests) == 2
        candidate = response.candidates[0]
        assert candidate.aliases == ["HD 1", "HIP 2"]
        assert candidate.fields["rvz_radvel"] == {"value": 520.0, "unit": "km / s"}
        assert candidate.fields["pm_err_maj"]["value"] == 0.1
        assert candidate.photometry[0]["band"] == "V"
        assert (
            candidate.photometry[0]["fields"]["bibcode"]["value"]
            == "FictionalPhotometry"
        )
        assert "VOTABLE" in response.raw_response
